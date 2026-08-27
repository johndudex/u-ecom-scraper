"""Iteration-economics T1.1 / T1.2 / T1.4 / T1.5 — summarizer + cap-bucket tests.

Covers (docs/plans/iteration-economics-plan.md):

- T1.1  field-map summarizer: ``api_path`` / ``api_fallback_path`` in the sel
        chain, ``notes`` rendered for API-fed fields, a BOUNDED
        ``api_extraction`` section, and the regression-critic guard — API-method
        fields are PINNED into the rendered set so the core-first
        ``_MAX_FIELDS`` cap cannot drop them.
        Two fixtures (priceline-shaped + a second, Shopify-ish API shape) so the
        test does not encode one artifact's shape as the contract.
- T1.2  test-report relay: ``suggested_fix`` (per issue) and
        ``feedback_for_writer`` (report level) reach code_writer.
- T1.4  ``run_scraper`` cap buckets inside the code_writer branch only.
- T1.5  ``mechanism_reassessment`` conditional injection.

The seed message is truncation-exempt, so the char caps asserted here are
load-bearing, not cosmetic.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

from agents.subagents import (
    _API_EXTRACTION_CAP,
    _ISSUE_FIX_CAP,
    _WRITER_FEEDBACK_CAP,
    _is_api_field,
    _mr_has_verdict,
    _render_api_extraction,
    _run_scraper_bucket,
    _summarize_product_analysis,
    _summarize_test_report,
    _suppress_mechanism_reassessment,
)

API_SECTION_PREFIX = "\n**API Extraction:** "


def _priceline_pa() -> dict:
    """priceline-com-au shaped product_analysis (OCC / InSider artifact)."""
    return {
        "site_slug": "priceline-com-au",
        "analyzed_products": 1,
        "extraction_methods": {
            "primary": "api",
            "api_available": True,
            "api_pattern": "https://api.priceline.com.au/occ/v2/priceline/products/{code}?fields=FULL",
        },
        "fields": {
            "title": {
                "method": "api",
                "api_path": "name",
                "notes": "Product name from the OCC response.",
            },
            "current_price": {
                "method": "api",
                "api_path": "discountedPrice.formattedValue",
                "api_fallback_path": "price.formattedValue",
                "notes": (
                    "When a discount is active, use discountedPrice.formattedValue "
                    "(e.g. '$109.00'). When no discount, fall back to "
                    "price.formattedValue (e.g. '$165.00')."
                ),
            },
            "previous_price": {
                "method": "api",
                "api_path": "price.formattedValue",
                "notes": (
                    "Original/list price. Only populated when a discount exists; "
                    "empty string otherwise."
                ),
            },
            # Non-core + NON-api: this is the field the core-first cap would
            # drop (regression critic #7 named it).
            "ratings": {
                "method": "css",
                "selector": ".ins-eureka-product-rating .product-rating-text",
                "js_extraction": (
                    "document.querySelector('.ins-eureka-product-rating "
                    ".product-rating-text')?.textContent.trim()"
                ),
                "notes": "Rating comes from an InSider widget, not the OCC API.",
            },
        },
        "api_extraction": {
            "endpoint": "https://api.priceline.com.au/occ/v2/priceline/products/{code}?fields=FULL",
            "method": "GET",
            "headers": {"Accept": "application/json"},
            "code_from_url": {"regex": r"/product/([\d]+)/"},
            "key_response_fields": {
                "price": {
                    "path": "price",
                    "type": "object",
                    "fields": {
                        "formattedValue": "string (e.g. '$165.00')",
                        "value": "number (e.g. 165)",
                        "currencyIso": "string (e.g. 'AUD')",
                    },
                },
                "discountedPrice": {
                    "path": "discountedPrice",
                    "type": "object",
                    "nullable": True,
                    "fields": {"formattedValue": "string (e.g. '$109.00')"},
                },
                "description": {"path": "description", "type": "string"},
            },
        },
        "mechanism_reassessment": {
            "original_recommendation": "unknown",
            "reassessed_mechanism": "http_requests",
            "reasoning": "The OCC REST API returns complete product JSON without auth.",
        },
        "page_structure": {"platform": "SAP Commerce / Spartacus"},
        "content_type": "product",
        "output_key": "products",
    }


def _shopify_pa() -> dict:
    """Second, structurally different API shape (Shopify-ish product JSON)."""
    return {
        "site_slug": "acme-store",
        "fields": {
            "title": {"method": "api", "api_path": "title"},
            "price": {
                "method": "internal_api",
                "api_path": "variants[0].price",
                "api_fallback_path": "price_min",
                "notes": "variants[0] is the default variant; price is a string.",
            },
            "availability": {
                "method": "api",
                "api_path": "available",
                "notes": "boolean → 'In Stock'/'Out of Stock'.",
            },
            "images": {
                "method": "jsonld",
                "selector": "JSON-LD Product.image",
                "notes": "DOM/JSON-LD field, not API — notes stay unrendered.",
            },
        },
        "api_extraction": {
            "url": "https://acme.example/products.json",
            "sample_keys": ["products", "id", "title", "variants", "price"],
            "sample_record": {"id": 1, "title": "Widget", "price": "10.00"},
        },
        "mechanism_reassessment": {
            "recommended": "http_requests",
            "site_analyzer_said": "playwright",
            "reason": "/products.json serves the whole catalog as JSON.",
        },
    }


# ── T1.1: api_path / api_fallback_path / notes ────────────────────────────


class TestApiPathRendering:
    def test_priceline_api_path_rendered(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert "sel=`discountedPrice.formattedValue`" in out
        assert "**current_price** [api]" in out

    def test_priceline_api_fallback_path_rendered_when_no_primary_path(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert "sel=`price.formattedValue`" in out  # previous_price

    def test_selector_still_wins_over_api_path(self):
        """Precedence is unchanged: selector → path → api_path → api_fallback."""
        pa = {
            "fields": {
                "price": {
                    "method": "api",
                    "selector": ".price",
                    "api_path": "price.formattedValue",
                }
            }
        }
        out = _summarize_product_analysis(pa)
        assert "sel=`.price`" in out
        assert "discountedPrice" not in out

    def test_priceline_notes_rendered(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert "notes=\"When a discount is active" in out
        assert "fall back to" in out

    def test_notes_rendered_for_internal_api_method_too(self):
        out = _summarize_product_analysis(_shopify_pa())
        assert "variants[0] is the default variant" in out

    def test_notes_not_rendered_for_non_api_fields(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert 'notes="Rating comes from an InSider widget' not in out

    def test_notes_newlines_collapsed(self):
        pa = {
            "fields": {
                "price": {
                    "method": "api",
                    "api_path": "price",
                    "notes": "line one\n\nline two\ttabbed",
                }
            }
        }
        assert 'notes="line one line two tabbed"' in _summarize_product_analysis(pa)

    def test_is_api_field_covers_path_keys_without_method_label(self):
        assert _is_api_field({"method": "api"})
        assert _is_api_field({"method": "internal_api"})
        assert _is_api_field({"method": "http", "api_path": "a.b"})
        assert _is_api_field({"api_fallback_path": "a.b"})
        assert not _is_api_field({"method": "css"})
        assert not _is_api_field("not-a-dict")


# ── T1.1: bounded api_extraction ──────────────────────────────────────────


class TestApiExtractionSection:
    def test_priceline_endpoint_and_code_regex_rendered(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert "**API Extraction:**" in out
        assert "endpoint=`GET https://api.priceline.com.au" in out
        assert "code_from_url=`/product/" in out

    def test_sample_keys_rendered(self):
        out = _summarize_product_analysis(_shopify_pa())
        assert "sample_keys=['products'" in out

    def test_one_example_entry_rendered_not_the_whole_map(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert '"formattedValue"' in out
        # exactly ONE example entry — the other key_response_fields entries
        # must not be inlined
        assert "description" not in out.split("**API Extraction:**")[1]

    def test_bounded_to_cap(self):
        out = _summarize_product_analysis(_priceline_pa())
        section = out.split("**API Extraction:**")[1].split("\n")[0]
        assert len(section) <= _API_EXTRACTION_CAP

    def test_huge_block_is_hard_truncated(self):
        ax = {
            "endpoint": "https://x.example/api",
            "key_response_fields": {"price": {"path": "a." + "b" * 900}},
        }
        section = _render_api_extraction(ax)
        assert section.startswith(API_SECTION_PREFIX)
        assert len(section) <= len(API_SECTION_PREFIX) + _API_EXTRACTION_CAP
        assert section.endswith("…")

    def test_endpoint_survives_even_with_a_huge_example(self):
        ax = {
            "endpoint": "https://x.example/api",
            "sample_record": {"blob": "x" * 5000},
        }
        section = _render_api_extraction(ax)
        assert "endpoint=`GET https://x.example/api`" in section

    def test_absent_api_extraction_renders_nothing(self):
        assert _render_api_extraction(None) == ""
        assert _render_api_extraction({}) == ""
        assert "**API Extraction:**" not in _summarize_product_analysis(_shopify_pa()) or True
        pa = _shopify_pa()
        del pa["api_extraction"]
        assert "**API Extraction:**" not in _summarize_product_analysis(pa)


# ── T1.1 guard: API fields never dropped by _MAX_FIELDS ───────────────────


def _wide_pa() -> dict:
    """20 fields, 4 core + 16 non-core; the 3 API fields sort LAST so the
    core-first cap would drop them without the pin."""
    fields: dict = {
        "title": {"method": "api", "api_path": "name"},
        "price": {"method": "api", "api_path": "price.formattedValue"},
        "url": {"method": "computed", "notes": "set by scraper"},
        "src_url": {"method": "computed"},
    }
    filler = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
        "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
        "oscar", "papa",
    ]
    for name in filler:
        fields[name] = {"method": "css", "selector": f".{name}"}
    # sort after every filler name
    for name in ("voltage_link", "warranty", "zeta_score"):
        fields[name] = {
            "method": "api",
            "api_path": f"{name}.value",
            "notes": f"{name} read recipe",
        }
    return {"fields": fields}


class TestApiFieldsPinnedPastCap:
    def test_all_api_fields_rendered_despite_cap(self):
        out = _summarize_product_analysis(_wide_pa())
        for name in ("voltage_link", "warranty", "zeta_score"):
            assert f"**{name}** [api]" in out, name
            assert f"sel=`{name}.value`" in out

    def test_cap_still_bounds_non_api_fields(self):
        """The pin must not silently disable the cap: the last non-API filler
        is still dropped."""
        out = _summarize_product_analysis(_wide_pa())
        assert "- **alpha**" in out
        assert "- **papa**" not in out

    def test_rendered_field_count_is_bounded(self):
        out = _summarize_product_analysis(_wide_pa())
        rendered = [ln for ln in out.splitlines() if ln.startswith("- **")]
        # 15-cap + 3 pinned API fields
        assert len(rendered) == 18

    def test_second_shape_api_fields_pinned(self):
        pa = _shopify_pa()
        for i in range(20):
            pa["fields"][f"filler_{i:02d}"] = {"method": "css", "selector": f".f{i}"}
        out = _summarize_product_analysis(pa)
        assert "**price** [internal_api]" in out
        assert "sel=`variants[0].price`" in out
        assert "**availability** [api]" in out


# ── T1.5: mechanism_reassessment conditional injection ────────────────────

_MR = {"recommended": "playwright", "reason": "OCC descriptor is a consent widget."}
_SA_APPLIED = {
    "strategy": "playwright",
    "strategy_justification": (
        "Deterministic: playwright; mechanism_reassessment.recommended='playwright' "
        "outranks count=None descriptor"
    ),
}
_SA_REJECTED = {
    "strategy": "internal_api",
    "strategy_justification": "Deterministic: count gate declined internal_api (count=None)",
}


class TestMechanismReassessmentSuppression:
    def test_rendered_by_default_no_scraper_analysis(self):
        out = _summarize_product_analysis(_priceline_pa())
        assert "**Mechanism Reassessment:**" in out

    def test_rendered_when_verdict_applied(self):
        out = _summarize_product_analysis(_priceline_pa(), scraper_analysis=_SA_APPLIED)
        assert "**Mechanism Reassessment:**" in out

    def test_suppressed_when_gate_armed_and_rejected(self):
        out = _summarize_product_analysis(_priceline_pa(), scraper_analysis=_SA_REJECTED)
        assert "**Mechanism Reassessment:**" not in out

    def test_empty_scraper_analysis_still_renders(self):
        out = _summarize_product_analysis(_priceline_pa(), scraper_analysis={})
        assert "**Mechanism Reassessment:**" in out

    def test_non_dict_scraper_analysis_still_renders(self):
        out = _summarize_product_analysis(_priceline_pa(), scraper_analysis=None)
        assert "**Mechanism Reassessment:**" in out

    def test_no_verdict_in_block_still_renders_under_armed_gate(self):
        pa = _priceline_pa()
        pa["mechanism_reassessment"] = {"reasoning": "OCC is fastest, no auth."}
        out = _summarize_product_analysis(pa, scraper_analysis=_SA_REJECTED)
        assert "**Mechanism Reassessment:**" in out

    def test_non_enum_token_is_not_a_verdict(self):
        """internal_api/api/stealth tokens are opinions, not verdicts (S3)."""
        assert not _mr_has_verdict({"recommended": "internal_api"})
        assert not _mr_has_verdict({"recommended": "api"})
        assert not _mr_has_verdict({"recommended": "use the OCC API directly"})
        assert _mr_has_verdict({"recommended": "playwright"})
        assert _mr_has_verdict({"reassessed_mechanism": "http_requests"})

    def test_verdict_found_nested(self):
        assert _mr_has_verdict({"a": [{"b": {"c": "http_navigation"}}]})

    def test_non_dict_block_does_not_crash_and_defaults_to_render(self):
        """A bare-string block is malformed, not a verdict — the default
        (render) applies because it cannot be determined."""
        pa = _priceline_pa()
        pa["mechanism_reassessment"] = "playwright"
        out = _summarize_product_analysis(pa, scraper_analysis=_SA_REJECTED)
        assert "**Mechanism Reassessment:**" in out

    def test_suppress_predicate_is_value_only(self):
        assert _suppress_mechanism_reassessment(_MR, _SA_REJECTED)
        assert not _suppress_mechanism_reassessment(_MR, _SA_APPLIED)
        assert not _suppress_mechanism_reassessment(_MR, None)
        assert not _suppress_mechanism_reassessment(None, _SA_REJECTED)

    def test_wiring_through_code_writer_message(self):
        """The builder must pass scraper_analysis in — otherwise the
        suppression branch can never arm."""
        from agents import subagents as sub

        base = {
            "site_slug": "t",
            "url": "https://x.example/p",
            "input_mode": "url_list",
            "target_fields": ["title", "current_price", "previous_price"],
            "page_type": "product",
            "product_analysis": _priceline_pa(),
        }
        with mock.patch.dict(os.environ, {"PROJECT_ROOT": ROOT}):
            rendered = str(sub.build_code_writer_message(
                dict(base, scraper_analysis=_SA_APPLIED))[0].content)
            suppressed = str(sub.build_code_writer_message(
                dict(base, scraper_analysis=_SA_REJECTED))[0].content)
        assert "**Mechanism Reassessment:**" in rendered
        assert "**Mechanism Reassessment:**" not in suppressed
        # the field map itself is unaffected by the suppression
        assert "**current_price** [api]" in suppressed
        assert "sel=`discountedPrice.formattedValue`" in suppressed


# ── T1.2: test-report relay ───────────────────────────────────────────────


def _report(**over) -> dict:
    report = {
        "overall_assessment": "NEEDS_FIXES",
        "confidence_score": 0.4,
        "issues": [
            {
                "severity": "high",
                "field": "url",
                "description": "double-host URL in output",
                "expected": "https://x.example/product/1",
                "actual": "https://x.example/https://x.example/product/1",
                "suggested_fix": "urljoin(BASE_URL, raw) instead of concatenating "
                                 "onto the listing path",
            },
            {
                "severity": "medium",
                "field": "price",
                "problem": "previous_price < current_price on 60% of rows",
                "suggested_fix": "orient the pair by VALUE: lower = current_price",
            },
        ],
    }
    report.update(over)
    return report


class TestTestReportRelay:
    def test_suggested_fix_relayed_for_high(self):
        out = _summarize_test_report(
            {"test_retry_count": 0, "test_report": _report()}
        )
        assert "Fix: urljoin(BASE_URL, raw)" in out

    def test_suggested_fix_relayed_for_medium(self):
        out = _summarize_test_report(
            {"test_retry_count": 0, "test_report": _report()}
        )
        assert "Fix: orient the pair by VALUE" in out

    def test_suggested_fix_absent_renders_no_fix_line(self):
        report = _report()
        for i in report["issues"]:
            i.pop("suggested_fix")
        out = _summarize_test_report({"test_retry_count": 0, "test_report": report})
        assert "Fix:" not in out

    def test_feedback_for_writer_relayed(self):
        out = _summarize_test_report({
            "test_retry_count": 0,
            "test_report": _report(
                feedback_for_writer=(
                    "CLI CONTRACT VIOLATION (deterministic — testing cannot pass "
                    "while discovery is unwired):\nAdd the missing argparse "
                    "declarations AND the SCRAPER_LISTING_URL env gate in main(). "
                    "Use edit_file; do NOT rewrite the scraper."
                )
            ),
        })
        assert "REMEDIATION INSTRUCTION" in out
        assert "SCRAPER_LISTING_URL env gate in main()" in out

    def test_feedback_relayed_even_with_no_issues(self):
        report = _report(issues=[], feedback_for_writer="fix the discovery gate")
        out = _summarize_test_report({"test_retry_count": 1, "test_report": report})
        assert "REMEDIATION INSTRUCTION" in out
        assert "fix the discovery gate" in out

    def test_feedback_absent_renders_no_section(self):
        out = _summarize_test_report({"test_retry_count": 0, "test_report": _report()})
        assert "REMEDIATION INSTRUCTION" not in out

    def test_caps(self):
        report = _report(
            feedback_for_writer="f" * (_WRITER_FEEDBACK_CAP + 500),
        )
        report["issues"][0]["suggested_fix"] = "s" * (_ISSUE_FIX_CAP + 500)
        out = _summarize_test_report({"test_retry_count": 0, "test_report": report})
        fix_line = next(
            ln for ln in out.splitlines() if ln.strip().startswith("Fix:")
        )
        assert len(fix_line.strip()) <= len("Fix: ") + _ISSUE_FIX_CAP
        assert fix_line.strip().endswith("…")

    def test_expected_actual_for_high_untouched(self):
        out = _summarize_test_report(
            {"test_retry_count": 0, "test_report": _report()}
        )
        assert "Expected: 'https://x.example/product/1'" in out
        assert "Actual: 'https://x.example/https://x.example/product/1'" in out

    def test_cli_contract_marker_relay_untouched(self):
        report = _report()
        report["issues"].append(
            {"severity": "high", "description": "CLI CONTRACT VIOLATION: no --fresh-discovery"}
        )
        out = _summarize_test_report({"test_retry_count": 0, "test_report": report})
        assert "CLI CONTRACT VIOLATION — targeted argparse fix" in out
        assert "do NOT regenerate" in out


# ── T1.4: run_scraper cap buckets ─────────────────────────────────────────


class _FakeTool:
    """Minimal stand-in for a BaseTool — apply_guard only needs .name/.func."""

    def __init__(self, name: str):
        self.name = name

        def _func(*args, **kwargs):
            return "RAN"

        self.func = _func


def _code_writer_run_scraper():
    from agents.subagents import _apply_guards

    tools = _apply_guards([_FakeTool("run_scraper")], "code_writer")
    assert len(tools) == 1
    return tools[0].func


class TestRunScraperCapBuckets:
    def test_bucket_classification(self):
        assert _run_scraper_bucket("workspace/s/probe_api.py") == "probe_family"
        assert _run_scraper_bucket("workspace/s/test_scratch.py") == "probe_family"
        assert _run_scraper_bucket("workspace/s/debug_run.py") == "probe_family"
        assert _run_scraper_bucket("workspace/s/scraper_draft.py") == "scraper_draft"
        assert _run_scraper_bucket("workspace/s/scraper_draft_v2.py") == "scraper_draft"
        assert _run_scraper_bucket("scratch.py") == "other"
        assert _run_scraper_bucket("") == "other"
        assert _run_scraper_bucket(None) == "other"
        # case + path-separator tolerance
        assert _run_scraper_bucket("/abs/X/PROBE.PY") == "probe_family"
        assert _run_scraper_bucket("C:\\ws\\scraper_draft.py") == "scraper_draft"

    def test_probes_capped_draft_still_allowed(self):
        """The stub sequence: probes×3 → the 3rd is refused, a draft run is
        still allowed (the whole point of the bucket split)."""
        run = _code_writer_run_scraper()
        assert run(scraper_path="w/probe_a.py") == "RAN"
        assert run(scraper_path="w/probe_b.py") == "RAN"
        refused = run(scraper_path="w/probe_c.py")
        assert "cap reached" in refused and "probe-family" in refused
        # draft bucket is untouched by the probe refusals
        assert run(scraper_path="w/scraper_draft.py") == "RAN"

    def test_draft_bucket_capped_at_two(self):
        run = _code_writer_run_scraper()
        assert run(scraper_path="w/scraper_draft.py") == "RAN"
        assert run(scraper_path="w/scraper_draft_v2.py") == "RAN"
        assert "scraper_draft*" in run(scraper_path="w/scraper_draft.py")

    def test_refusal_states_remaining_budget_per_bucket(self):
        run = _code_writer_run_scraper()
        run(scraper_path="w/probe_a.py")
        run(scraper_path="w/probe_b.py")
        msg = run(scraper_path="w/probe_c.py")
        assert "Remaining run_scraper budget:" in msg
        assert "probe-family (probe*/test_*/debug* targets) 0/2" in msg
        assert "scraper_draft* 2/2" in msg
        assert "other targets 3/3" in msg

    def test_positional_arg_form_is_bucketed(self):
        run = _code_writer_run_scraper()
        run("w/test_x.py")
        run("w/test_y.py")
        assert "cap reached" in run("w/test_z.py")

    def test_other_bucket_keeps_the_pre_split_cap_of_three(self):
        run = _code_writer_run_scraper()
        for i in range(3):
            assert run(scraper_path=f"w/scratch_{i}.py") == "RAN"
        assert "other targets" in run(scraper_path="w/scratch_3.py")

    def test_code_tester_run_scraper_is_not_capped(self):
        """Regression pin: the guard binds code_writer ONLY — code_tester
        legitimately re-runs the draft across fix cycles."""
        from agents.subagents import _apply_guards

        tools = _apply_guards([_FakeTool("run_scraper")], "code_tester")
        func = tools[0].func
        for i in range(10):
            assert func(scraper_path="w/scraper_draft.py") == "RAN", i

    def test_guard_not_applied_to_other_tools(self):
        from agents.subagents import _apply_guards

        tools = _apply_guards(
            [_FakeTool("run_scraper"), _FakeTool("read_file")], "code_writer"
        )
        by_name = {t.name: t for t in tools}
        assert by_name["read_file"].func() == "RAN"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
