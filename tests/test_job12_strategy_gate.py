"""Job-12 fix plan S2/S3/S4/S6-retention — strategy gate, precedence, escalation.

Locks, on recorded descriptors from the real forensics corpus (see
tests/fixtures/endpoints/README.md):

- S2: the internal_api override requires POSITIVE count evidence
  (``count`` a positive int), not merely "not explicitly zero".
  items_per_page>0 is nearly vacuous (verify_api returns no descriptor
  unless it found a non-empty dict-array, then ipp=len>=1) — count is the
  gate's one real discriminator.
- S3: an explicit mechanism verdict from content/product analysis
  (``mechanism_reassessment.recommended``) outranks a count:null
  descriptor — but can never re-arm internal_api past the count gate.
- S4: escalation never re-picks a tried+failed strategy, never escalates
  INTO internal_api without evidence, and routes to the exhausted path
  (cleanup under skip_approvals, else human_approval) when no untried
  strategy remains — instead of silently re-running the same one
  (job-12 cycle 3: 10 min / 49 tool calls / zero writes).
- S6: the descriptor projection retains the measured evidence
  (sample_keys / content_type) that the fetch already produced.
- Marker fix: ``_invoke_agent_with_timeout`` preserves the exception CLASS.
"""

import json
import os

import pytest

from agents.graph import (
    _derive_strategy,
    _escalate_strategy,
    _invoke_agent_with_timeout,
    _project_api_endpoint,
)

FIXTURES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "endpoints",
)

API_DESCRIPTOR_STRATEGIES = ("http_requests", "http_navigation", "playwright")


def _load_fixtures():
    paths = sorted(
        os.path.join(FIXTURES_DIR, f)
        for f in os.listdir(FIXTURES_DIR)
        if f.endswith(".json")
    )
    assert len(paths) >= 8, f"expected >=8 endpoint fixtures, found {len(paths)}"
    return [json.load(open(p, encoding="utf-8")) for p in paths]


def _state_for(nav, *, rendering=None, recommended=None, rec_key="content_analysis"):
    """Build a minimal state for _derive_strategy around a navigation_analysis."""
    if rendering:
        nav = dict(nav)
        nav["rendering_verified"] = rendering
    state = {
        "url": "https://test.com",
        "site_slug": "test-com",
        "probe_result": {"connectivity": {"method_that_worked": "direct_http"}},
        "navigation_analysis": nav,
        "input_mode": "navigation",
    }
    if recommended is not None:
        state[rec_key] = {
            "mechanism_reassessment": {"recommended": recommended},
        }
    return state


# ── S2: golden replay over the recorded corpus ────────────────────────────


@pytest.mark.parametrize(
    "fixture_name",
    [
        "ketchcdn-consent-config.json",
        "useinsider-personalization.json",
        "aya-jobs-api.json",
        "amn-jobs-api.json",
        "coveo-explicit-zero.json",
        "zquiet-heatmap.json",
        "sidley-taxonomy.json",
        "shopify-feed-legit-no-total.json",
    ],
)
def test_golden_replay_fixture_verdicts(fixture_name):
    """Every recorded descriptor must produce its expected gate verdict."""
    with open(os.path.join(FIXTURES_DIR, fixture_name), encoding="utf-8") as f:
        fx = json.load(f)
    analysis = _derive_strategy(_state_for(fx["navigation_analysis"]))
    got = analysis["strategy"] == "internal_api"
    assert got == fx["expected_internal_api"], (
        f"{fixture_name}: expected_internal_api={fx['expected_internal_api']}, "
        f"got strategy={analysis['strategy']!r} — {fx.get('note', '')[:120]}"
    )


def test_gate_requires_positive_count_not_merely_nonzero():
    """count=null is the poison signature (ketchcdn/useinsider/zquiet/sidley)."""
    nav = {
        "data_source": "api",
        "rendering_verified": "browser",
        "api_endpoint": {
            "url": "https://widget.example.com/api/info",
            "count": None,
            "items_per_page": 5,
        },
    }
    assert _derive_strategy(_state_for(nav))["strategy"] != "internal_api"


def test_gate_positive_count_still_passes():
    """The aya class (real total reported) must keep firing internal_api."""
    nav = {
        "data_source": "api",
        "rendering_verified": "browser",
        "api_endpoint": {
            "url": "https://test.com/api/jobs/search",
            "count": 26955,
            "items_per_page": 20,
        },
    }
    assert _derive_strategy(_state_for(nav))["strategy"] == "internal_api"


def test_gate_bool_count_is_not_positive_evidence():
    """bool is an int subclass — True must not masquerade as count>0."""
    nav = {
        "data_source": "api",
        "rendering_verified": "browser",
        "api_endpoint": {
            "url": "https://test.com/api/search",
            "count": True,
            "items_per_page": True,
        },
    }
    assert _derive_strategy(_state_for(nav))["strategy"] != "internal_api"


# ── S3: mechanism_reassessment precedence ─────────────────────────────────


def test_explicit_recommendation_beats_null_count_descriptor():
    """Job-12 shape: poison descriptor + product_analysis says playwright.

    rendering=csr would cascade to http_navigation; the explicit verdict
    (which for priceline carried OCC-interception instructions) wins.
    """
    nav = {
        "data_source": "api",
        "rendering_verified": "csr",
        "api_endpoint": {
            "url": "https://pricelineau.api.useinsider.com/api/info/824.24",
            "count": None,
            "items_per_page": 1,
        },
    }
    analysis = _derive_strategy(
        _state_for(nav, recommended="playwright")
    )
    assert analysis["strategy"] == "playwright"
    assert "mechanism_reassessment" in analysis["strategy_justification"]


def test_recommendation_honored_from_product_analysis_key_too():
    """content_analysis AND product_analysis both carry the artifact
    (gradual migration) — the precedence rule must consult both."""
    nav = {
        "data_source": "api",
        "rendering_verified": "csr",
        "api_endpoint": {"url": "https://t.com/api", "count": None, "items_per_page": 3},
    }
    analysis = _derive_strategy(
        _state_for(nav, recommended="playwright", rec_key="product_analysis")
    )
    assert analysis["strategy"] == "playwright"


def test_recommendation_cannot_rearm_internal_api():
    """A recommendation is OPINION; the count gate is MEASUREMENT. A
    descriptor that fails the count gate stays failed even when the
    analysis recommends internal_api/api (poison descriptors report
    opinions too)."""
    nav = {
        "data_source": "api",
        "rendering_verified": "browser",
        "api_endpoint": {
            "url": "https://pricelineau.api.useinsider.com/api/info/824.24",
            "count": None,
            "items_per_page": 1,
        },
    }
    for rec in ("internal_api", "api"):
        assert _derive_strategy(_state_for(nav, recommended=rec))[
            "strategy"
        ] != "internal_api"


def test_positive_count_beats_missing_or_conflicting_recommendation():
    """Measured evidence outranks opinion in the other direction too: a
    positive count fires internal_api even if the analysis prefers a
    browser strategy."""
    nav = {
        "data_source": "api",
        "rendering_verified": "browser",
        "api_endpoint": {
            "url": "https://test.com/api/jobs/search",
            "count": 26955,
            "items_per_page": 20,
        },
    }
    for rec in (None, "http_requests"):
        assert (
            _derive_strategy(_state_for(nav, recommended=rec))["strategy"]
            == "internal_api"
        )


def test_no_recommendation_cascade_unchanged():
    """Without an explicit verdict, the rendering cascade is untouched."""
    nav = {
        "data_source": "none",
        "rendering_verified": "csr",
        "api_endpoint": {},
    }
    assert _derive_strategy(_state_for(nav))["strategy"] == "http_navigation"


# ── S4: escalation honesty ────────────────────────────────────────────────


def _analysis_for(strategy):
    return {
        "strategy": strategy,
        "scraping_mechanism": strategy,
        "scraping_method": strategy,
        "recommended_strategy": strategy,
        "strategy_justification": "Deterministic: test",
    }


def test_no_escalation_when_strategy_untried():
    analysis, goto = _escalate_strategy(_analysis_for("playwright"), set())
    assert goto is None
    assert analysis["strategy"] == "playwright"


def test_escalation_still_climbs_to_untried_strategy():
    """http_navigation failed → playwright is still a valid escalation."""
    analysis, goto = _escalate_strategy(
        _analysis_for("http_navigation"), {"http_navigation"}
    )
    assert goto is None
    assert analysis["strategy"] == "playwright"


def test_escalation_from_playwright_exhausts_without_internal_api():
    """A failed browser strategy must not escalate INTO internal_api —
    evidence-backed internal_api is CHOSEN by the gate, never escalated
    into (_ESCALATION[3:] was the hole: a failed playwright re-picked
    internal_api with no evidence check). From playwright the only upward
    rung is internal_api, so the ladder is exhausted → routed out, and no
    downward rung is manufactured."""
    analysis, goto = _escalate_strategy(_analysis_for("playwright"), {"playwright"})
    assert goto == "human_approval"
    assert analysis["strategy"] == "playwright"
    assert "exhausted" in analysis["strategy_justification"]


def test_internal_api_failure_routes_to_cleanup_under_skip_approvals():
    """Job-12 cycle 3: internal_api tried+failed, _ESCALATION[4:] empty —
    the old code re-picked internal_api (10-min zero-write thrash). Now:
    skip_approvals intake jobs route to cleanup (honest failure)."""
    analysis, goto = _escalate_strategy(
        _analysis_for("internal_api"), {"internal_api"}, skip_approvals=True
    )
    assert goto == "cleanup"
    assert "exhausted" in analysis["strategy_justification"]


def test_internal_api_failure_routes_to_human_approval_with_approvals():
    analysis, goto = _escalate_strategy(_analysis_for("internal_api"), {"internal_api"})
    assert goto == "human_approval"


def test_fully_exhausted_ladder_routes_out_instead_of_re_picking():
    """Every rung tried (internal_api excluded) → exhausted routing, never
    a same-strategy re-pick."""
    tried = {"http_requests", "http_navigation", "playwright"}
    analysis, goto = _escalate_strategy(
        _analysis_for("playwright"), tried, skip_approvals=True
    )
    assert goto == "cleanup"
    assert analysis["strategy"] == "playwright"  # unchanged, but routed out


# ── S6: descriptor evidence retention ─────────────────────────────────────


def test_api_endpoint_projection_retains_sample_keys_and_content_type():
    """graph.py:2393 must stop discarding the measured evidence the fetch
    produced — sample_keys/content_type survive into navigation_analysis."""
    from agents.graph import _project_api_endpoint

    api = {
        "url": "https://www.sidley.com/sitecore/api/people/search",
        "count": None,
        "items_per_page": 100,
        "sample_keys": ["text", "value", "count"],
        "content_type": "application/json",
        "sample_params": {"q": "x"},
    }
    projected = _project_api_endpoint(api)
    assert projected["url"] == api["url"]
    assert projected["count"] is None
    assert projected["items_per_page"] == 100
    assert projected["sample_keys"] == ["text", "value", "count"]
    assert projected["content_type"] == "application/json"


def test_api_endpoint_projection_handles_missing_and_empty():
    from agents.graph import _project_api_endpoint

    assert _project_api_endpoint(None) == {}
    assert _project_api_endpoint({}) == {}
    # No url -> passthrough (legacy behavior preserved)
    legacy = {"api_url": "https://t.com/api", "count": 5}
    assert _project_api_endpoint(legacy) == legacy


# ── Marker fix: exception class preserved ─────────────────────────────────


class _FakeRateLimited(Exception):
    pass


def test_invoke_timeout_preserves_exception_class():
    class _BoomAgent:
        def invoke(self, *a, **k):
            raise _FakeRateLimited("Error code: 429 - Rate limit reached")

    box = _invoke_agent_with_timeout(
        _BoomAgent(), [{"role": "user", "content": "hi"}], {}, "test", "job-x", timeout=5
    )
    assert box.get("_error_class") == "_FakeRateLimited"
    assert "429" in box.get("_error", "")


def test_invoke_timeout_sync_path_empty_on_clean_empty():
    class _EmptyAgent:
        def invoke(self, *a, **k):
            return {"messages": []}

    box = _invoke_agent_with_timeout(
        _EmptyAgent(), [{"role": "user", "content": "hi"}], {}, "test", "job-x", timeout=5
    )
    assert box == {"messages": []}
