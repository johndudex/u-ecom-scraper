"""Iteration-economics plan v2 — regression pins for the core-file half.

Covers (docs/plans/iteration-economics-plan.md):
- T0.2/T0.3: wall-clock timeouts are env-configurable and DISTINGUISHABLE
  (``_error_class="WallClockTimeout"``) — a dead invocation is no longer
  indistinguishable from budget exhaustion.
- T0.4: a dead code_writer invocation with NO draft routes PAST code_tester
  (scraper_analyzer bounce, bounded by a dedicated counter → human_approval).
- T0.5/H1: string-``site`` outputs no longer destroy the ground-truth
  overrides (shared normalizer).
- T1.5 (graph half): mechanism_reassessment is read by verdict keys + a
  bounded value scan that EXCLUDES origin-marking keys — the analyzer
  restating the old verdict cannot flip strategy to the thing it argued
  against.
- T2.1: the volume gate is SILENT on a job-302-shaped sample run
  (PASS / 5 extracted / 97 discovered → still field_confirmation) and arms
  only beyond sample scope.
- T2.2: deterministic WRONG_VALUE issues with an anchored suggested_fix block
  both the PASS branch and the ground-truth override.
"""

from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from langgraph.types import RunnableConfig  # noqa: E402

import agents.graph as g  # noqa: E402

# agents.nodes re-exports the function as `route_after_testing`, shadowing the
# submodule attribute — importlib guarantees we get the MODULE for patching.
rat = importlib.import_module("agents.nodes.route_after_testing")


# ─── T0.2/T0.3: wall-clock honesty ───────────────────────────────────────────


class TestWallClockTimeoutShape:
    def test_env_configurable_timeout(self, monkeypatch):
        assert g._env_int("ANY_MISSING_VAR", 7) == 7
        monkeypatch.setenv("ANY_MISSING_VAR", "1200")
        assert g._env_int("ANY_MISSING_VAR", 7) == 1200
        monkeypatch.setenv("ANY_MISSING_VAR", "garbage")
        assert g._env_int("ANY_MISSING_VAR", 7) == 7

    def test_timeout_returns_wall_clock_error_class(self, monkeypatch):
        """A timed-out invoke is DISTINCT from budget exhaustion."""
        import threading as _th

        started = _th.Event()

        class _Stuck:
            def invoke(self, *a, **k):
                started.set()
                _th.Event().wait(timeout=1.0)
                return {"messages": ["late"]}

        monkeypatch.setattr(g, "_async_execution_enabled", lambda: False)
        result = g._invoke_agent_with_timeout(
            _Stuck(), [], {}, "code_writer", 0, timeout=0
        )
        assert result.get("_error_class") == "WallClockTimeout"
        assert result.get("_error")
        assert result.get("messages") == []


class TestDeadWriterRouting:
    """T0.4: dead invocation + no draft → route PAST code_tester."""

    def _run(self, monkeypatch, tmp_path, state, agent_result):
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "set_tool_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "clear_tool_context", lambda: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(g, "build_code_writer_message", lambda state: [])
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "_start_heartbeat", lambda *a, **k: 0)
        monkeypatch.setattr(g, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(g, "_agent_config", lambda *a, **k: {})
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "_select_template_file", lambda state: "requests_scraper.py")
        monkeypatch.setattr(g, "_PATCHES_ENABLED", False, raising=False)

        class _Stub:
            def invoke(self, *a, **k):
                return agent_result

        monkeypatch.setattr(g, "create_code_writer", lambda site_slug="", **_: _Stub())
        return g._invoke_code_writer(state, RunnableConfig())

    def test_dead_invocation_no_draft_bounces_to_scraper_analyzer(self, monkeypatch, tmp_path):
        state = {"job_id": 0, "site_slug": "t", "code_writer_error_count": 0}
        cmd = self._run(monkeypatch, tmp_path, state, {"messages": []})
        assert cmd.goto == "scraper_analyzer"
        assert (cmd.update or {}).get("code_writer_error_count") == 1
        assert (cmd.update or {}).get("code_writer_error")

    def test_second_consecutive_death_escalates_to_human(self, monkeypatch, tmp_path):
        state = {"job_id": 0, "site_slug": "t", "code_writer_error_count": 1}
        cmd = self._run(monkeypatch, tmp_path, state, {"messages": []})
        assert cmd.goto == "human_approval"
        assert (cmd.update or {}).get("interrupt_reason") == "code_writer_failed"

    def test_exception_result_also_counts_as_dead(self, monkeypatch, tmp_path):
        state = {"job_id": 0, "site_slug": "t", "code_writer_error_count": 0}
        cmd = self._run(
            monkeypatch, tmp_path, state,
            {"messages": [], "_error": "429 too many requests", "_error_class": "RateLimitError"},
        )
        assert cmd.goto == "scraper_analyzer"
        assert "429" in (cmd.update or {}).get("code_writer_error", "")

    def test_dead_invocation_with_draft_continues(self, monkeypatch, tmp_path):
        """Timeout hit AFTER the draft was written — the draft is usable."""
        from langgraph.types import Command as _Command

        ws = tmp_path / "workspace" / "t"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text("print('x')\n")
        for name in ("_warn_unaddressed_critical_fix", "_fix_scraper_syntax",
                     "_enforce_cli_contract", "_enforce_discovery_import",
                     "_enforce_env_discovery_gate", "_patch_scraper_output_filter"):
            monkeypatch.setattr(g, name, lambda *a, **k: None, raising=False)
        monkeypatch.setattr(g, "_PATCHES_ENABLED", False, raising=False)
        state = {"job_id": 0, "site_slug": "t", "code_writer_error_count": 0,
                 "scraper_analysis": {"strategy": "http_requests"}}
        result = self._run(monkeypatch, tmp_path, state, {"messages": []})
        assert not isinstance(result, _Command)
        assert result.get("scraping_method") == "http_requests"

    def test_resume_routing_for_code_writer_failed(self):
        assert g.route_from_human_approval({
            "interrupt_reason": "code_writer_failed",
            "human_response": {"decision": "approve", "label": "Retry code generation"},
        }) == "scraper_analyzer"
        assert g.route_from_human_approval({
            "interrupt_reason": "code_writer_failed",
            "human_response": {"decision": "reject", "label": "Cancel"},
        }) == "__end__"


# ─── T0.5/H1: string-site normalizer ─────────────────────────────────────────


class TestSiteBlockNormalizer:
    def test_string_site_normalized_with_fallback(self):
        from src.output_site import normalize_site_block

        block = normalize_site_block("www.priceline.com.au", fallback_platform="sfcc")
        assert block["platform"] == "sfcc"
        assert block["name"] == "www.priceline.com.au"

    def test_dict_site_passthrough_with_backfill(self):
        from src.output_site import normalize_site_block

        block = normalize_site_block({"platform": "shopify", "name": "x"})
        assert block == {"platform": "shopify", "name": "x"}
        assert normalize_site_block({"name": "x"}, fallback_platform="sfcc")["platform"] == "sfcc"

    def test_tasks_ground_truth_uses_normalizer(self):
        """Source pin (finalize_job needs a live DB to run): the ground-truth
        block must go through the shared normalizer — a bare
        ``out_data.get("site", {}).get("platform")`` AttributeError'd on the
        string-`site` outputs and the bare except discarded EVERY override."""
        import inspect

        import scraper.tasks as tasks

        src = inspect.getsource(tasks)
        assert "normalize_site_block" in src
        assert "ground_truth_platform" in src


# ─── T1.5 (graph half): mechanism_reassessment reading ───────────────────────


class TestMechanismReassessment:
    def _strategy(self, mr):
        state = {
            "probe_result": {"connectivity": {"method_that_worked": "browser_none"}},
            "product_analysis": {"mechanism_reassessment": mr},
        }
        return g._derive_strategy(state)

    def test_verdict_key_outranks_origin_key(self):
        """reassessed_mechanism=http_requests wins even though the artifact
        restates the OLD verdict (playwright) under original_recommendation."""
        analysis = self._strategy({
            "original_recommendation": "playwright",
            "reassessed_mechanism": "http_requests",
            "reasoning": "OCC intercepts Playwright",
        })
        assert analysis["strategy"] == "http_requests"

    def test_legacy_recommended_key_still_wins(self):
        analysis = self._strategy({"recommended": "playwright"})
        assert analysis["strategy"] == "playwright"
        assert "recommended" in analysis["strategy_justification"]

    def test_value_scan_excludes_origin_marking_keys(self):
        """No verdict key present; the only non-origin enum value is picked."""
        analysis = self._strategy({
            "site_analyzer_said": "playwright",
            "verdict": "http_navigation",
        })
        assert analysis["strategy"] == "http_navigation"

    def test_ambiguous_values_ignored(self):
        analysis = self._strategy({"a": "playwright", "b": "http_requests"})
        assert analysis["strategy"] == "http_navigation"  # cascade verdict stands

    def test_internal_api_never_re_armed_by_opinion(self):
        state = {
            "probe_result": {"connectivity": {"method_that_worked": "direct_http"}},
            "navigation_analysis": {"data_source": "api", "api_endpoint": {"url": "https://t/api", "items_per_page": 5, "count": None}},
            "product_analysis": {"mechanism_reassessment": {"recommended": "internal_api"}},
        }
        analysis = g._derive_strategy(state)
        assert analysis["strategy"] != "internal_api"


# ─── T2.1/T2.2: routing arbitration pins ─────────────────────────────────────


def _job302_state(overrides: dict | None = None, report_overrides: dict | None = None):
    """The job-302 shape: tester PASS / 0.9 / sample-5 against 97 discovered."""
    report = {
        "overall_assessment": "PASS",
        "confidence_score": 0.9,
        "issues": [],
        "results": {"successful_extractions": 5, "sample_size": 5},
        "phases_tested": {"phase1_discovery": True},
        "discovery_coverage": {"ran_phase1": True, "found": 97, "stop_reason": "max_pages_hit"},
    }
    report.update(report_overrides or {})
    state = {
        "site_slug": "t",
        "input_mode": "list_page",
        "page_type": "product",
        "scope": "",
        "scope_value": "",
        "test_retry_count": 0,
        "test_report": report,
        "navigation_analysis": {"api_endpoint": {"items_per_page": 36}},
    }
    state.update(overrides or {})
    return state


class TestRouteArbitration:
    def test_job302_sample_run_still_ships(self, monkeypatch):
        """REGRESSION PIN: the volume gate must NOT fail the sample run that
        succeeded (the v1 fatal flaw)."""
        assert rat.route_after_testing(_job302_state()) == "field_confirmation"

    def test_beyond_sample_volume_gap_bounces(self, monkeypatch):
        report_over = {"results": {"successful_extractions": 30}}
        state = _job302_state(report_overrides=report_over)
        # [A5] the volume-gap bounce targets code_writer directly — the
        # strategy prompt adds LLM latency without addressing a volume defect.
        assert rat.route_after_testing(state) == "code_writer"

    def test_deterministic_wrong_value_blocks_pass_and_override(self, monkeypatch):
        """A double-host WRONG_VALUE with an anchored fix is a known mechanical
        defect — neither the PASS branch nor the ground-truth override ships it."""
        report_over = {
            "issues": [{
                "field": "url",
                "issue_type": "WRONG_VALUE",
                "severity": "medium",
                "description": "36/36 item URLs contain the host twice",
                "suggested_fix": "Join with urljoin(base_url, raw_url).",
            }],
        }
        # [A5] deterministic mechanical defects target code_writer directly.
        assert rat.route_after_testing(_job302_state(report_overrides=report_over)) == (
            "code_writer"
        )

    def test_unanchored_llm_wrong_value_stays_advisory(self, monkeypatch):
        """Without suggested_fix, an LLM's WRONG_VALUE opinion does not arm the
        bounce (severity-floor regression guard)."""
        report_over = {
            "issues": [{
                "field": "url",
                "issue_type": "WRONG_VALUE",
                "severity": "medium",
                "description": "some URLs look odd",
            }],
        }
        assert rat.route_after_testing(_job302_state(report_overrides=report_over)) == (
            "field_confirmation"
        )

    def test_src_url_listing_never_flaggable(self, monkeypatch):
        report_over = {
            "issues": [{
                "field": "src_url",
                "issue_type": "WRONG_VALUE",
                "severity": "medium",
                "description": "src_url is the listing URL",
                "suggested_fix": "should be the detail page",
            }],
        }
        # src_url is not in the blocker field set — two-phase by-design.
        assert rat.route_after_testing(_job302_state(report_overrides=report_over)) == (
            "field_confirmation"
        )


class TestDeterministicChecks:
    def test_double_host_and_price_inversion_detected(self, monkeypatch, tmp_path):
        rows = [
            {
                "title": f"p{i}",
                "url": "https://www.x.com.auhttps://www.x.com.au/c/gifts/p{i}",
                "price": "$5.00",
                "previous_price": "$4.00",  # inverted (prev < current)
                "rating": "",  # mapped but empty
            }
            for i in range(5)
        ]
        out = tmp_path / "output_1.json"
        import json as _json

        out.write_text(_json.dumps({"products": rows, "metadata": {}}))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        _json.dump({"products": rows, "metadata": {}},
                   open(tmp_path / "workspace" / "s" / "output_test.json", "w"))
        state = {
            "content_analysis": {
                "fields": {
                    "title": {"selector": ".title"},
                    "rating": {"selector": ".stars"},
                }
            }
        }
        issues = rat.deterministic_output_issues("s", state)
        fields = {(i["field"], i["issue_type"]) for i in issues}
        assert ("url", "WRONG_VALUE") in fields
        assert ("price", "WRONG_VALUE") in fields
        assert ("rating", "MISSING") in fields
        for i in issues:
            assert i["severity"] == "medium"
            assert i.get("suggested_fix")

    def test_quiet_on_few_rows(self, monkeypatch, tmp_path):
        import json as _json

        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        _json.dump({"products": [{"title": "only one"}]},
                   open(tmp_path / "workspace" / "s" / "output_test.json", "w"))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        assert rat.deterministic_output_issues("s", {}) == []

    def test_rating_mapped_from_count_fires(self, monkeypatch, tmp_path):
        """Job-303: ratings mapped at numberOfReviews — the 2 review-bearing
        products shipped their review COUNT as the star value."""
        import json as _json

        rows = [
            {"title": f"p{i}", "url": f"https://x.com/p{i}",
             "ratings": "1" if i < 2 else ""}
            for i in range(5)
        ]
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        _json.dump({"products": rows, "metadata": {}},
                   open(tmp_path / "workspace" / "s" / "output_test.json", "w"))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        state = {"product_analysis": {"fields": {
            "ratings": {"method": "embedded_json",
                        "json_path": "cx-state.product.details.value.numberOfReviews",
                        "notes": "Number of reviews (integer)."},
        }}}
        issues = rat.deterministic_output_issues("s", state)
        hit = [i for i in issues
               if i["field"] == "ratings" and i["issue_type"] == "WRONG_VALUE"]
        assert hit, f"expected rating-from-count issue, got {issues}"
        assert hit[0]["severity"] == "medium"
        assert "averageRating" in hit[0]["suggested_fix"]
        # and it must be a routing blocker
        blockers = [
            i for i in issues
            if str(i.get("issue_type")).upper() == "WRONG_VALUE"
            and (i.get("suggested_fix") or "").strip()
            and str(i.get("field") or "").lower()
            in ("url", "price", "previous_price", "original_price",
                "ratings", "rating", "average_rating")
        ]
        assert blockers

    def test_rating_mapped_to_value_stays_quiet(self, monkeypatch, tmp_path):
        """Correct map (averageRating) + sparse fills = legitimate — no issue."""
        import json as _json

        rows = [
            {"title": f"p{i}", "url": f"https://x.com/p{i}",
             "ratings": "4.5" if i < 2 else ""}
            for i in range(5)
        ]
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        _json.dump({"products": rows, "metadata": {}},
                   open(tmp_path / "workspace" / "s" / "output_test.json", "w"))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        state = {"product_analysis": {"fields": {
            "ratings": {"method": "embedded_json",
                        "json_path": "cx-state.product.details.value.averageRating",
                        "notes": "Average star value; empty when no reviews."},
        }}}
        assert rat.deterministic_output_issues("s", state) == []

    def test_declining_notes_mentioning_count_stays_quiet(self, monkeypatch, tmp_path):
        """Job-306: the analyzer honestly DECLINED to map ratings (api_path=None,
        notes explain 'only has numberOfReviews (a count, not a rating value)').
        'notes' is prose, not an anchor — mentioning a count there must not fire
        the count-map check on an artifact that maps nothing."""
        import json as _json

        rows = [
            {"title": f"p{i}", "url": f"https://x.com/p{i}",
             "ratings": "1" if i < 2 else ""}
            for i in range(5)
        ]
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        _json.dump({"products": rows, "metadata": {}},
                   open(tmp_path / "workspace" / "s" / "output_test.json", "w"))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        state = {"product_analysis": {"fields": {
            "ratings": {"method": "api", "api_path": None, "tested": True,
                        "examples": [],
                        "notes": ("The API response has no averageRating/ratingValue"
                                  " field. It only has numberOfReviews (a count, not"
                                  " a rating value). Per the >=3-samples rule this"
                                  " field is NOT available.")},
        }}}
        assert rat.deterministic_output_issues("s", state) == []

    def test_stale_previous_job_output_does_not_feed_checks(self, monkeypatch, tmp_path):
        """Job-306 root cause: the F16 best-of-N picker ranked the PREVIOUS job's
        36-row production output above this run's 5-row sample, so the checks
        validated against the wrong job's rows. An mtime floor (the job's
        started_at) must exclude stale files."""
        import json as _json
        import os as _os

        big = [{"title": f"old{i}", "url": f"https://x.com/old{i}",
                "price": "$5.00", "ratings": "1"}
               for i in range(36)]
        small = [{"title": f"new{i}", "url": f"https://x.com/new{i}",
                  "price": "$6.00", "ratings": ""}
                 for i in range(5)]
        ws = tmp_path / "workspace" / "s"
        prod = tmp_path / "scrapers" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        prod.mkdir(parents=True, exist_ok=True)
        _json.dump({"products": small, "metadata": {}},
                   open(ws / "output_new.json", "w"))
        _json.dump({"products": big, "metadata": {}},
                   open(prod / "output_old.json", "w"))
        # prod file: much larger AND much older than the workspace file
        _old = (_os.path.getmtime(str(ws / "output_new.json")) - 86_400 * 7)
        _os.utime(str(prod / "output_old.json"), (_old, _old))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

        # legacy (no floor): big stale file wins — documents the trap
        assert len(rat._items_from_output_file("s")) == 36
        # floored at "job start" (now-ish): only the fresh sample qualifies
        import time as _time

        floor = _time.time() - 60
        assert len(rat._items_from_output_file("s", mtime_floor=floor)) == 5
