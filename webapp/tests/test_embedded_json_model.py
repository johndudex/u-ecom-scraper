"""Unit tests for the embedded-JSON listing data model (plan Parts 2+3).

Exercises the generic detector, the data-richest-listing promotion, the
navigate_synthesize propagation, and the code_writer prompt precedence — all
deterministically, with no network and no browser.

Run inside the Django container::

    docker compose exec django pytest webapp/tests/test_embedded_json_model.py -q
"""

import json
import os
import sys

# Make `agents`, `scraper`, `config` importable (webapp/ on sys.path) and ensure
# Django is configured before importing modules that read django.conf.settings.
_WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WEBAPP not in sys.path:
    sys.path.insert(0, _WEBAPP)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

if not getattr(django, "_setup_done_", False):
    django.setup()


def _records(n=5, **extra):
    """n homogeneous record-like dicts (>=3 primitive keys each)."""
    base = {"id": 1, "title": "Job Title", "city": "Austin", "state": "TX"}
    base.update(extra)
    return [dict(base, id=i) for i in range(1, n + 1)]


# ── _raw_html_has_embedded_json (Part 2c) ───────────────────────────────────


def test_raw_html_detects_inline_window_assignment():
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    blob = json.dumps(_records(6, jobID=1))
    html = f"<html><body><script>window.ayaSearchMenusInitialState.jobsData = {blob};</script></body></html>"
    assert _raw_html_has_embedded_json(html) is True


def test_raw_html_detects_next_data():
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    data = {"props": {"pageProps": {"jobs": _records(7)}}}
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'
    assert _raw_html_has_embedded_json(html) is True


def test_raw_html_detects_jsonld_flat_product_array():
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    prods = [{"@type": "Product", "name": f"P{i}", "sku": str(i), "price": "9.99"} for i in range(5)]
    html = f'<html><head><script type="application/ld+json">{json.dumps(prods)}</script></head></html>'
    assert _raw_html_has_embedded_json(html) is True


def test_raw_html_rejects_plain_marketing_page():
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    html = '<html><body><a href="/about">About</a><script>console.log("hi"); init();</script></body></html>'
    assert _raw_html_has_embedded_json(html) is False


def test_raw_html_rejects_non_homogeneous_or_too_small():
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    # only 2 records
    small = json.dumps(_records(2))
    assert _raw_html_has_embedded_json(f"<script>x = {small};</script>") is False
    # non-homogeneous (no shared keys)
    messy = [{"a": 1, "b": 2, "c": 3}, {"d": 4, "e": 5, "f": 6}, {"g": 7, "h": 8, "i": 9}]
    assert _raw_html_has_embedded_json(f"<script>y = {json.dumps(messy)};</script>") is False


def test_raw_html_hint_filter_uses_locator_token():
    """When locator hints are given, the hint guides the scan (precision)."""
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    blob = json.dumps(_records(4))
    html = f"<script>var jobsData = {blob};</script>"
    assert _raw_html_has_embedded_json(html, locator_hints=["jobsData"]) is True


def test_detector_picks_item_array_over_larger_taxonomy():
    """Real aya scenario: jobsData(10 real jobs) must beat expertises(110
    specialties). The 'largest record array' would wrongly pick the taxonomy."""
    from agents.nodes.navigate_explore import _find_best_record_array, _raw_html_has_embedded_json

    jobs = [{"jobID": i, "facilityName": "f", "city": "c", "stateCode": 1,
             "expertiseText": "e", "professionText": "p", "weeklyPayLow": 1}
            for i in range(10)]
    specialties = [{"id": i, "name": "spec" + str(i), "abbreviation": "s", "professionIds": []}
                   for i in range(110)]
    # both arrays present (as on the nursing category page)
    html = f"<script>var jobsData = {json.dumps(jobs)}; var expertises = {json.dumps(specialties)};</script>"
    assert _raw_html_has_embedded_json(html) is True

    best = {"count": 0, "score": -1}
    from agents.nodes.navigate_explore import _balanced_substr
    import re as _re
    for m in _re.finditer(r"var\s+(\w+)\s*=\s*\[", html):
        sub = _balanced_substr(html, html.find("[", m.start()))
        try:
            _find_best_record_array(json.loads(sub), best, path=m.group(1))
        except Exception:
            pass
    assert best["count"] == 10, f"expected jobsData(10), got {best}"
    assert best["path"] == "jobsData"


def test_detector_rejects_page_with_only_taxonomy():
    """Marketing page with only specialties/professions (no item array) → False."""
    from agents.nodes.navigate_explore import _raw_html_has_embedded_json

    specialties = [{"id": i, "name": "s" + str(i), "abbreviation": "a", "professionIds": []} for i in range(110)]
    html = f"<script>var expertises = {json.dumps(specialties)};</script>"
    assert _raw_html_has_embedded_json(html) is False


def test_balanced_substr_handles_nested_brackets_and_strings():
    from agents.nodes.navigate_explore import _balanced_substr

    txt = 'x = [ {"a": "h]llo", "b": [1,2]}, {"c": "}"} ]'
    open_idx = txt.index("[")
    sub = _balanced_substr(txt, open_idx)
    assert sub is not None
    # must parse back to a list (balanced extraction respected nested brackets/quotes)
    assert isinstance(json.loads(sub), list)


# ── _data_score + _promote_data_richest_listing (Part 2b) ───────────────────


def test_data_score_prefers_embedded_record_count():
    from agents.nodes.navigate_explore import _data_score

    assert _data_score({"data_source": "embedded_json", "embedded_json": {"best": {"record_count": 50}}}) == 50
    assert _data_score({"data_source": "detail_links", "product_links": [{"href": "a"}, {"href": "b"}, {"href": "c"}]}) == 3
    assert _data_score({}) == 0


def test_promote_rescues_thin_listing_with_embedded_snapshot():
    from agents.nodes.navigate_explore import _promote_data_richest_listing

    rich_listing = {
        "url": "https://site/healthcare-jobs/nursing/",
        "data_source": "embedded_json",
        "embedded_json": {"best": {"record_count": 120}},
        "product_links": [],
    }
    findings = {
        "listing_page": {"url": "https://site/travel-nursing/", "data_source": "none", "product_links": []},
        "_listing_snapshots": {"https://site/healthcare-jobs/nursing/": {
            "score": 120, "data_source": "embedded_json", "listing": rich_listing,
        }},
    }
    _promote_data_richest_listing(findings)
    assert findings["listing_page"]["url"] == "https://site/healthcare-jobs/nursing/"
    assert findings["listing_page"]["promoted_from"] == "https://site/healthcare-jobs/nursing/"
    assert findings["listing_page"]["data_source"] == "embedded_json"


def test_promote_does_not_override_good_detail_listing():
    """A detail-link listing that already found items is not overridden by a
    smaller/lower one (no regression for normal shops)."""
    from agents.nodes.navigate_explore import _promote_data_richest_listing

    findings = {
        "listing_page": {"url": "https://site/shop", "data_source": "detail_links",
                         "product_links": [{"href": str(i)} for i in range(8)]},
        "_listing_snapshots": {"https://site/other": {
            "score": 5, "data_source": "detail_links",
            "listing": {"url": "https://site/other", "data_source": "detail_links",
                        "product_links": [{"href": str(i)} for i in range(5)]},
        }},
    }
    _promote_data_richest_listing(findings)
    assert findings["listing_page"]["url"] == "https://site/shop"  # unchanged


def test_promote_embedded_beats_detail():
    """An embedded-JSON page (whole dataset) outranks a detail-link page."""
    from agents.nodes.navigate_explore import _promote_data_richest_listing

    embedded_listing = {
        "url": "https://site/cat", "data_source": "embedded_json",
        "embedded_json": {"best": {"record_count": 200}}, "product_links": [],
    }
    findings = {
        "listing_page": {"url": "https://site/search", "data_source": "detail_links",
                         "product_links": [{"href": str(i)} for i in range(8)]},
        "_listing_snapshots": {"https://site/cat": {
            "score": 200, "data_source": "embedded_json", "listing": embedded_listing,
        }},
    }
    _promote_data_richest_listing(findings)
    assert findings["listing_page"]["url"] == "https://site/cat"


def test_promote_noop_without_snapshots():
    from agents.nodes.navigate_explore import _promote_data_richest_listing

    findings = {"listing_page": {"url": "x", "product_links": []}}
    _promote_data_richest_listing(findings)
    assert findings["listing_page"]["url"] == "x"


# ── _fallback_synthesize propagation (Part 3a) ──────────────────────────────


def test_synthesize_propagates_embedded_json_signal(tmp_path):
    from agents.nodes.navigate_synthesize import _fallback_synthesize

    slug = "ayasite"
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True)
    findings = {
        "homepage_nav": {"category_links": [
            {"href": "https://site/healthcare-jobs/nursing/", "text": "Nursing"},
        ]},
        "listing_page": {
            "url": "https://site/healthcare-jobs/nursing/",
            "product_links": [],  # modal-only: no detail links
            "data_source": "embedded_json",
            "data_richness": 120,
            "embedded_json": {"detected": True, "best": {
                "kind": "inline_script", "locator": "ayaSearchMenusInitialState.jobsData",
                "record_count": 120, "sample_keys": ["jobID", "title", "city"],
                "sample_record": {"jobID": 1, "title": "t"},
            }},
            "embedded_json_reachable_via": "raw_html",
            "rendering_verified": "ssr",
        },
        "metadata": {"search_criteria": "nursing"},
    }
    (ws / "navigation_findings.json").write_text(json.dumps(findings))

    state = {"site_slug": slug, "job_id": 1, "url": "https://site", "search_criteria": "nursing"}
    result = _fallback_synthesize(state, str(tmp_path), slug)
    na = result["navigation_analysis"]
    assert na["data_source"] == "embedded_json"
    assert na["embedded_json"]["best"]["record_count"] == 120
    assert na["rendering_verified"] == "ssr"


# ── build_code_writer_message precedence (Part 3d) ──────────────────────────


def _base_state(slug="ejsite", **overrides):
    state = {
        "site_slug": slug,
        "url": "https://site.example",
        "product_url": "https://site.example/listing",
        "input_mode": "navigation",
        "page_type": "job_posting",
        "search_criteria": "nursing",
        "content_type_config": {"content_type": "job_posting", "output_key": "jobs",
                                "fields": [{"name": "title", "required": True}]},
        "site_analysis": {},
        "scraper_analysis": {"strategy": "http_requests", "proxy_tier": "none",
                             "data_source": "embedded_json"},
        "navigation_analysis": {
            "data_source": "embedded_json",
            "embedded_json": {"detected": True, "best": {
                "kind": "inline_script", "locator": "ayaSearchMenusInitialState.jobsData",
                "record_count": 120, "array_path": "ayaSearchMenusInitialState.jobsData",
                "sample_keys": ["jobID", "title", "city", "state"],
                "sample_record": {"jobID": 1, "title": "RN", "city": "Austin", "state": "TX"},
            }},
            "search": {"working_url": "https://site.example/healthcare-jobs/nursing/"},
            "categories": {"category_links": ["https://site.example/healthcare-jobs/nursing/",
                                              "https://site.example/healthcare-jobs/allied/"]},
            "discovery_method": "category",
        },
        "output_schema": {},
    }
    state.update(overrides)
    return state


def test_code_writer_emits_embedded_json_block_when_no_api():
    from agents.subagents import build_code_writer_message

    msg = build_code_writer_message(_base_state())[0].content
    assert "EMBEDDED-JSON LISTING data model" in msg
    assert "map_jobs" in msg  # generic job field resolver
    assert "ayaSearchMenusInitialState.jobsData" in msg  # detected locator surfaced
    # the embedded-JSON block precedes (supersedes) the generic two-phase text
    assert msg.index("EMBEDDED-JSON LISTING data model") < msg.index("TWO-PHASE")


def test_code_writer_api_takes_precedence_over_embedded_json():
    """When a backend API was discovered, the API block wins (amn case)."""
    from agents.subagents import build_code_writer_message

    na = _base_state()["navigation_analysis"]
    na["api_endpoint"] = {
        "url": "https://api.site.example/Job/search?PageNumber=1",
        "base": "https://api.site.example/Job/search",
        "method": "GET", "query_params": ["PageNumber"], "pagination_param": "PageNumber",
        "page_size_param": "PageSize", "has_pagination": True,
    }
    state = _base_state(navigation_analysis=na)
    msg = build_code_writer_message(state)[0].content
    assert "Backend JSON search API discovered" in msg
    assert "EMBEDDED-JSON LISTING data model" not in msg  # embedded block skipped


def test_code_writer_falls_back_to_two_phase_without_embedded_signal():
    """Detail-link site (no embedded signal, no API) → plain two-phase text."""
    from agents.subagents import build_code_writer_message

    state = _base_state()
    state["scraper_analysis"]["data_source"] = "detail_links"
    state["navigation_analysis"]["data_source"] = "detail_links"
    state["navigation_analysis"]["embedded_json"] = {"detected": False, "best": None}
    state["navigation_analysis"]["item_links"] = {"url_examples": ["https://site.example/jobs/1"]}
    msg = build_code_writer_message(state)[0].content
    assert "EMBEDDED-JSON LISTING data model" not in msg
    assert "TWO-PHASE" in msg
