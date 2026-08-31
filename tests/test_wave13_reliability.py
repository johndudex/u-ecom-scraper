"""Wave-13 reliability fixes — regression tests.

Covers the fixes shipped in the wave-13 campaign:

- T3.13c  mechanical ``phase2_instant_fail`` detector (job-76 myhouse's
  0.81 s tell) — predicate semantics + both HTTP templates emitting the
  counter into ``metadata.discovery_coverage``;
- T2.7    ``src.field_verification`` — live-render field-mapping verification
  (selector stripping, path walking, verdicts) and its consumers
  (``validate_coverage`` proven-dead downgrade, string-verdict markers);
- T3.13i  the 85-gap-c probe-yield fix — a ``--discover-only`` probe's
  ``found`` is 0 BY CONSTRUCTION, so the graph nulls it before the
  ``listing_yield_failure`` predicate; healthy short catalogues must not be
  declared dead;
- T3.13h  ``[CASCADE]`` / ``[RESUME-INVOCATION]`` SessionLog observability rows;
- T3.13d  ``_remap_sample_urls`` — remap cycles re-verify against URLs the
  failed run actually touched, not the same single sample page;
- job-62  ``detect_soft_block`` env floor (``SCRAPER_SOFT_BLOCK_MIN_BYTES``).
"""

import ast
import importlib
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

# agents.* lives under webapp/ on PYTHONPATH; src.* at the repo root.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "webapp"))


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# T3.13c — phase2_instant_fail predicate (src.page_analysis)
# ═══════════════════════════════════════════════════════════════════════════════

from src.page_analysis import phase2_instant_fail  # noqa: E402


class TestPhase2InstantFailPredicate:
    def test_job76_tell_flags(self):
        # 40 items at a 2 s per-fetch delay cannot finish in 0.81 s.
        assert phase2_instant_fail(0.81, 40, 2.0) is True

    def test_healthy_serial_run_passes(self):
        assert phase2_instant_fail(90.0, 40, 2.0) is False

    def test_concurrent_run_scales_floor_by_workers(self):
        # floor = 40 * 0.5 * 0.5 / 4 = 2.5 s
        assert phase2_instant_fail(2.0, 40, 0.5, workers=4) is True
        assert phase2_instant_fail(4.0, 40, 0.5, workers=4) is False

    def test_floor_is_strict(self):
        # exactly at floor (1 * 2 * 0.5 = 1.0) is NOT a fail
        assert phase2_instant_fail(1.0, 1, 2.0) is False
        assert phase2_instant_fail(0.5, 1, 2.0) is True

    def test_unconfigured_delay_never_arms(self):
        assert phase2_instant_fail(0.0, 40, 0) is False
        assert phase2_instant_fail(0.0, 40, None) is False

    def test_zero_items_never_arms(self):
        assert phase2_instant_fail(0.0, 0, 2.0) is False

    def test_garbage_inputs_never_arms(self):
        assert phase2_instant_fail(None, None, None) is False
        assert phase2_instant_fail("x", "y", "z") is False


class TestTemplatesEmitPhase2InstantFail:
    def test_requests_template_emits_counter(self):
        src = _tpl("requests_scraper.py")
        assert "phase2_instant_fail" in src
        # the counter rides the discovery_coverage block, not a loose variable
        cov_at = src.index("discovery_coverage = {")
        block = src[cov_at:src.index("}", cov_at)]
        assert "phase2_instant_fail" in block
        # and the timing wraps the Phase-2 extraction loop
        assert "phase2_start" in src

    def test_http_navigation_template_emits_counter(self):
        src = _tpl("http_navigation_scraper.py")
        assert "phase2_instant_fail" in src
        cov_at = src.index("discovery_coverage = {")
        block = src[cov_at:src.index("}", cov_at)]
        assert "phase2_instant_fail" in block
        assert "PHASE2_MIN_FETCH_S" in src


# ═══════════════════════════════════════════════════════════════════════════════
# job-62 — detect_soft_block env floor
# ═══════════════════════════════════════════════════════════════════════════════

from src.http_fetch import SoftBlock, detect_soft_block  # noqa: E402


class TestSoftBlockDetector:
    def test_challenge_markers_always_detected(self):
        # the whole detector is OFF at the default floor of 0 (prod default) —
        # markers are only judged when a floor is staged
        with mock.patch.dict(os.environ, {"SCRAPER_SOFT_BLOCK_MIN_BYTES": "500"}):
            block = detect_soft_block(
                "<html>Access denied — verify you are human</html>"
            )
            assert block is not None
            assert block.reason == "challenge_marker"

    def test_env_floor_off_by_default(self):
        with mock.patch.dict(os.environ, {"SCRAPER_SOFT_BLOCK_MIN_BYTES": "0"}):
            assert detect_soft_block("<html>tiny</html>") is None

    def test_env_floor_flags_small_bodies(self):
        with mock.patch.dict(os.environ, {"SCRAPER_SOFT_BLOCK_MIN_BYTES": "500"}):
            block = detect_soft_block("<html>short body</html>")
            assert block is not None
            assert block.reason == "under_min_bytes"

    def test_env_floor_spares_normal_pages(self):
        with mock.patch.dict(os.environ, {"SCRAPER_SOFT_BLOCK_MIN_BYTES": "10"}):
            assert detect_soft_block("<html>" + "x" * 200 + "</html>") is None

    def test_softblock_is_falsy(self):
        # callers branch on ``if not result`` — a SoftBlock instance must be
        # falsy-yet-distinct (job-62: isinstance check, not truthiness)
        with mock.patch.dict(os.environ, {"SCRAPER_SOFT_BLOCK_MIN_BYTES": "500"}):
            block = detect_soft_block("unusual activity detected")
            assert not block
            assert isinstance(block, SoftBlock)


# ═══════════════════════════════════════════════════════════════════════════════
# T2.7 — src.field_verification
# ═══════════════════════════════════════════════════════════════════════════════

from src.field_verification import (  # noqa: E402
    _product_block,
    _resolve_via_render,
    _strip_jsonld_selector,
    _walk_path,
    verify_enabled,
)


class TestStripJsonldSelector:
    def test_strips_space_joined_prefix_and_type(self):
        # the analyzer writes "JSON-LD Product.offers.price" — the "JSON-LD"
        # tag is space-joined INSIDE the first dotted segment
        assert _strip_jsonld_selector("JSON-LD Product.offers.price") == "offers.price"

    def test_strips_dotted_type(self):
        assert _strip_jsonld_selector("Product.offers.price") == "offers.price"

    def test_keeps_plain_path(self):
        assert _strip_jsonld_selector("offers.price") == "offers.price"

    def test_case_insensitive_type_token(self):
        assert _strip_jsonld_selector("json-ld product.name") == "name"


class TestWalkPath:
    def test_nested_dicts(self):
        assert _walk_path({"a": {"b": 7}}, "a.b") == 7

    def test_numeric_list_segment(self):
        assert _walk_path({"h": [{"t": "x"}]}, "h.0.t") == "x"

    def test_missing_returns_none(self):
        assert _walk_path({"a": 1}, "a.b.c") is None

    def test_empty_path_returns_data(self):
        assert _walk_path({"a": 1}, "") == {"a": 1}


class TestProductBlock:
    TYPES = ("Product", "ProductGroup")

    def test_typed_match_beats_first(self):
        blocks = [{"@type": "WebSite"}, {"@type": "Product", "name": "P"}]
        assert _product_block(blocks, self.TYPES)["name"] == "P"

    def test_graph_wrappers_flattened(self):
        blocks = [{"@graph": [{"@type": "Product", "name": "G"}]}]
        assert _product_block(blocks, self.TYPES)["name"] == "G"

    def test_fallback_first_dict(self):
        blocks = [{"@type": "WebSite", "n": 1}]
        assert _product_block(blocks, self.TYPES)["n"] == 1

    def test_empty_returns_none(self):
        assert _product_block([], self.TYPES) is None


class TestResolveViaRender:
    HTML = '<html><body><h1 class="pdp">Widget</h1><span class="price">$9</span></body></html>'
    BLOCKS = [{"@type": "Product", "offers": {"price": "19.99"}, "name": "Widget"}]
    TYPES = ("Product",)

    def test_structured_data_verified(self):
        verdict, sample = _resolve_via_render(
            {"method": "structured_data", "selector": "JSON-LD Product.offers.price"},
            self.HTML, self.BLOCKS, self.TYPES,
        )
        assert verdict == "verified"
        assert sample == "19.99"

    def test_css_verified(self):
        verdict, sample = _resolve_via_render(
            {"method": "css", "selector": "span.price"},
            self.HTML, self.BLOCKS, self.TYPES,
        )
        assert verdict == "verified"
        assert sample == "$9"

    def test_dead_selector_is_empty(self):
        verdict, sample = _resolve_via_render(
            {"method": "css", "selector": ".does-not-exist"},
            self.HTML, self.BLOCKS, self.TYPES,
        )
        assert verdict == "empty"
        assert sample == ""

    def test_resolver_without_typed_block_is_skipped(self):
        # resolver paths may derive from API samples — no typed block means
        # UNJUDGEABLE, never "empty" (false alarm guard)
        verdict, _ = _resolve_via_render(
            {"method": "resolver", "selector": "price.amount"},
            self.HTML, [{"@type": "WebSite"}], self.TYPES,
        )
        assert verdict == "skipped"

    def test_api_method_is_skipped(self):
        verdict, _ = _resolve_via_render(
            {"method": "api", "selector": "data.price"},
            self.HTML, self.BLOCKS, self.TYPES,
        )
        assert verdict == "skipped"

    def test_css_fallback_rescues_empty_jsonld(self):
        verdict, sample = _resolve_via_render(
            {
                "method": "structured_data",
                "selector": "Product.missing.path",
                "css_fallback": "span.price",
            },
            self.HTML, self.BLOCKS, self.TYPES,
        )
        assert verdict == "verified"
        assert sample == "$9"


class TestVerifyEnabled:
    def test_default_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCRAPER_MAPPING_VERIFY", None)
            assert verify_enabled() is True

    def test_explicit_off(self):
        for v in ("0", "false", "no", "off", "OFF"):
            with mock.patch.dict(os.environ, {"SCRAPER_MAPPING_VERIFY": v}):
                assert verify_enabled() is False, v


# ═══════════════════════════════════════════════════════════════════════════════
# T3.13e — validate_coverage proven-dead downgrade
# ═══════════════════════════════════════════════════════════════════════════════

vc = importlib.import_module("agents.nodes.validate_coverage")


class TestCoverageProvenDeadDowngrade:
    def test_proven_dead_mapping_is_not_coverage(self):
        analysis = {
            "fields": {
                "title": {"method": "css", "selector": "h1", "tested": "empty"},
                "price": {"method": "css", "selector": ".price"},
            }
        }
        covered = vc._extract_covered_fields(analysis)
        assert "title" not in covered
        assert "price" in covered

    def test_unverified_mapping_keeps_presence_credit(self):
        # strictly gated: merely unverified / skipped fields are UNTOUCHED —
        # only the live-render PROVEN-DEAD verdict downgrades
        analysis = {
            "fields": {
                "title": {"method": "css", "selector": "h1", "tested": "verified"},
                "price": {"method": "css", "selector": ".price", "tested": "skipped"},
                "url": {"method": "css", "selector": "link"},  # never verified
            }
        }
        covered = vc._extract_covered_fields(analysis)
        assert covered == {"title", "price", "url"}

    def test_pre_t27_analysis_byte_identical(self):
        analysis = {"fields": {"title": {"method": "css", "selector": "h1"}}}
        assert vc._extract_covered_fields(analysis) == {"title"}

    def test_proven_dead_list(self):
        analysis = {
            "fields": {
                "title": {"method": "css", "selector": "h1", "tested": "empty"},
                "price": {"method": "css", "selector": ".price", "tested": "empty"},
                "url": {"method": "css", "selector": "link", "tested": "verified"},
            }
        }
        assert vc._proven_dead_fields(analysis) == ["price", "title"]

    def test_low_coverage_message_names_dead_sources(self, monkeypatch):
        analysis = {
            # only title covers (price proven dead) → 1/7 core < 80 %
            "fields": {
                "title": {"method": "css", "selector": "h1"},
                "price": {"method": "css", "selector": ".dead", "tested": "empty"},
            }
        }
        monkeypatch.setattr(vc, "_load_product_analysis", lambda slug: analysis)
        result = vc.validate_coverage(
            {"site_slug": "cov", "page_type": "product", "navigation_analysis": {}}
        )
        msg = result.update["interrupt_message"]
        assert "DEAD" in msg
        assert "price" in msg
        # the dead field is excluded from the remediation target: it must be
        # re-anchored, not "retried"
        assert sorted(result.update["test_report"]["remediation"]["fields"]) == [
            "availability", "currency", "original_price", "price", "src_url", "url",
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# T3.13i — probe-yield found-nulling (85-gap-c)
# ═══════════════════════════════════════════════════════════════════════════════

from src.listing_discovery import listing_yield_failure  # noqa: E402


class TestProbeYieldTwoPageCatalogue:
    def test_graph_nulled_probe_shape_is_not_dead(self):
        # job-85's own listing: 2-page catalogue ending no_next_link. The
        # graph nulls the nested coverage's found (0 BY CONSTRUCTION on a
        # --discover-only probe) before calling the predicate → alive.
        probe_yield = {
            "discovered_urls": 48,
            "coverage": {"found": None, "stop_reason": "no_next_link"},
        }
        assert listing_yield_failure(probe_yield) is False

    def test_literal_nested_zero_from_extractor_run_still_dead(self):
        # a run that EXTRACTED and still reported found=0 with exhaustion
        # flavor keeps the old zero-usable-yield verdict
        cov = {
            "discovered_urls": 48,
            "found": 0,
            "stop_reason": "no_next_link",
        }
        assert listing_yield_failure(cov) is True

    def test_real_extraction_zero_still_dead(self):
        cov = {"discovered_urls": 50, "found": 0, "stop_reason": "short_page"}
        assert listing_yield_failure(cov) is True

    def test_max_pages_hit_never_dead(self):
        cov = {"discovered_urls": 48, "found": 0, "stop_reason": "max_pages_hit"}
        assert listing_yield_failure(cov) is False


# ═══════════════════════════════════════════════════════════════════════════════
# T3.13h — [CASCADE] / [RESUME-INVOCATION] observability rows
# ═══════════════════════════════════════════════════════════════════════════════

rat = importlib.import_module("agents.nodes.route_after_testing")
ckt = importlib.import_module("agents.nodes.check_tracker")


class TestCascadeLogRows:
    def test_log_cascade_writes_row(self):
        rows = []
        graph_mod = importlib.import_module("agents.graph")
        with mock.patch.object(
            graph_mod, "_log_event_row", lambda job_id, node, content: rows.append(content)
        ):
            rat._log_cascade(
                {"job_id": 321}, "strategy-switch", "browser_traverse failed twice"
            )
        assert len(rows) == 1
        assert "[CASCADE]" in rows[0]
        assert "action=strategy-switch" in rows[0]
        assert "browser_traverse failed twice" in rows[0]

    def test_log_cascade_noop_without_job_id(self):
        rows = []
        graph_mod = importlib.import_module("agents.graph")
        with mock.patch.object(
            graph_mod, "_log_event_row", lambda job_id, node, content: rows.append(content)
        ):
            rat._log_cascade({}, "retry-no-report", "no test_report")
        assert rows == []


class TestResumeInvocationRow:
    def test_writes_resume_invocation_row(self):
        rows = []
        graph_mod = importlib.import_module("agents.graph")
        with mock.patch.object(
            graph_mod, "_log_event_row", lambda job_id, node, content: rows.append(content)
        ):
            ckt._log_resume_invocation(
                {"job_id": 322, "force_full": True, "input_mode": "navigation"},
                "complete", True,
            )
        assert len(rows) == 1
        assert "[RESUME-INVOCATION]" in rows[0]
        assert "site_status=complete" in rows[0]
        assert "force_full=True" in rows[0]
        assert "input_mode=navigation" in rows[0]

    def test_noop_without_job_id(self):
        rows = []
        graph_mod = importlib.import_module("agents.graph")
        with mock.patch.object(
            graph_mod, "_log_event_row", lambda job_id, node, content: rows.append(content)
        ):
            ckt._log_resume_invocation({}, "complete", False)
        assert rows == []


# ═══════════════════════════════════════════════════════════════════════════════
# T3.13d — _remap_sample_urls
# ═══════════════════════════════════════════════════════════════════════════════

subagents_mod = importlib.import_module("agents.subagents")


class TestRemapSampleUrls:
    def test_from_test_report_coverage_urls(self):
        state = {
            "test_report": {
                "discovery_coverage": {
                    "discovered_urls": [
                        "https://x.example/p/1",
                        "https://x.example/p/2",
                        "not-a-url",
                    ]
                }
            }
        }
        urls = subagents_mod._remap_sample_urls(state, "slug")
        assert urls == ["https://x.example/p/1", "https://x.example/p/2"]

    def test_cap_at_limit(self):
        state = {
            "test_report": {
                "discovery_coverage": {
                    "discovered_urls": [
                        f"https://x.example/p/{i}" for i in range(10)
                    ]
                }
            }
        }
        assert len(subagents_mod._remap_sample_urls(state, "slug")) == 5

    def test_dedupes_preserving_order(self):
        state = {
            "test_report": {
                "discovery_coverage": {
                    "discovered_urls": [
                        "https://x.example/p/1",
                        "https://x.example/p/1",
                        "https://x.example/p/2",
                    ]
                }
            }
        }
        assert subagents_mod._remap_sample_urls(state, "slug") == [
            "https://x.example/p/1",
            "https://x.example/p/2",
        ]

    def test_empty_when_nothing_available(self):
        assert subagents_mod._remap_sample_urls({}, "slug") == []
        assert subagents_mod._remap_sample_urls(
            {"test_report": {"discovery_coverage": {}}}, "slug"
        ) == []
