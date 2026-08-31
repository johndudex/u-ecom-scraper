"""[jobs 83 + 88] Wave-11 classes from the paired prod RCA.

Job 83 (woolworths) — the strategy layer ignored WHICH browser flavor the
probe proved. The probe blocked playwright at every tier it ran
(playwright_none / playwright_datacenter / playwright_residential) before
uc_chrome_none + uc_chrome_residential succeeded, yet ``_derive_strategy``'s
browser-render branch hard-coded ``playwright`` for GET-form listings — and
``_enforce_anti_bot_strategy`` deliberately leaves playwright unrewritten.
A stale ``test_report`` riding the graph checkpoint also burned the first of
three test cycles ("RETEST MODE (Cycle 2)" on the first test).

Job 88 (selfridges) — a working draft was destroyed. The requests draft
discovered 8 real product URLs and extracted 5/5 correct fields, but the
tester prompt's unbounded "> 1 page worth" volume bar flagged NEEDS_FIXES
(scope was firstn/10 — 80% of the ask), so the writer switched to a browser
template that discovered 0 and exited 0 with no output file. That exact run
shape was invisible to every rescue gate: RC1's ``_execution_zero_discovery``
needed an output file to read and ``_maybe_retry_execution_listing`` refused
FAILED results outright.

Fixes pinned here (generic, no caps):
- N1  ``_derive_strategy``: stealth-proven probe (uc_chrome*/cloak*) in the
  browser-render branch → ``http_navigation`` (probe-proven cloak server-side),
  never bare playwright. POST-form replay still wins (existing contract).
- N3  ``empty_render`` joins ``_COVERAGE_FAIL_STOP_REASONS`` — the empty-page
  soft wall is a give-up signal, so the anti-bot downgrade cannot swallow a
  tester strategy verdict over it.
- N4  parse_command nulls ``test_report`` on fresh runs; ``_invoke_code_writer``
  also ignores a truthy report whose file is gone (checkpoint outranks updates
  on resume paths).
- N5  playwright template warm-up survives HARD navigation errors (its goto has
  no per-item caller above it — anything it raises ends the whole run).
- N6  browser_traverse emits a ``[NAV-SUMMARY]`` row (it used to emit zero
  rows — neither RCA could be reconstructed from the job log).
- N7  the tester volume assertion carries the same firstn/filter waiver as the
  deterministic ``_volume_gap`` gate.
- N8  RC1 is reachable for the clean rc=0-no-output run (``no_fresh_output``).
- N9  http_navigation zero discovery exits 3 with a ``DISCOVERY_ZERO`` stderr
  marker instead of an unobservable exit-0.
- N12 input_urls.json preserve check tolerates a bare JSON array.
- Partial-draft floor: a dead writer invocation's draft must parse (ast) or it
  counts as no draft.

Run: docker compose exec -T django python -m pytest tests/test_job83_job88_classes.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

# Job 83: PDP url, the real listing only in search_criteria (firstn/10).
JOB83_PDP = "https://www.woolworths.com.au/shop/productdetails/734634/woolworths-bananas"
JOB83_LISTING = "https://www.woolworths.com.au/shop/browse/fruit-veg"
# Job 88: same intake shape.
JOB88_PDP = "https://www.selfridges.com/US/en/product/love-by-design-geneva-v-neck-jumper_R04061278/"
JOB88_LISTING = "https://www.selfridges.com/GB/en/categories/women/swimwear-beachwear/"

GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _graph_src() -> str:
    with open(GRAPH, encoding="utf-8") as f:
        return f.read()


# ─── N1: strategy follows the browser flavor the probe PROVED ───────────────


class TestDeriveStrategyProbeFlavor:
    def _state(self, method="uc_chrome_residential", form="GET", rendering="browser"):
        return {
            "url": JOB83_PDP,
            "probe_result": {
                "connectivity": {"method_that_worked": method},
                "anti_bot": {"detected": True},
            },
            "navigation_analysis": {
                "rendering_verified": rendering,
                "search": {"form_method": form},
            },
        }

    def test_stealth_probe_never_picks_playwright(self):
        """The job-83 shape: playwright blocked at every probed tier, uc_chrome
        proven. Bare playwright must not be derived."""
        from webapp.agents.graph import _derive_strategy

        for method in ("uc_chrome_none", "uc_chrome_residential", "cloak_none"):
            analysis = _derive_strategy(self._state(method=method))
            assert analysis["strategy"] == "http_navigation", (method, analysis["strategy"])

    def test_unproven_browser_still_picks_playwright(self):
        """Regression lock: playwright stays the browser-render default when the
        probe did NOT prove a stealth flavor (no evidence against it)."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(self._state(method="browser_none"))
        assert analysis["strategy"] == "playwright"

    def test_form_post_replay_still_wins(self):
        """Existing contract: POST-form discovery is HTTP-replayable regardless
        of probe flavor (locumtenens class)."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(self._state(form="POST"))
        assert analysis["strategy"] == "http_requests"


# ─── N3: empty_render is a give-up signal ────────────────────────────────────


class TestEmptyRenderCoverage:
    def test_empty_render_in_the_fail_set(self):
        from webapp.agents.nodes.route_after_testing import _COVERAGE_FAIL_STOP_REASONS

        assert "empty_render" in _COVERAGE_FAIL_STOP_REASONS

    def test_empty_render_report_fails_the_coverage_gate(self):
        """The exact job-83 tester report: discovery ran, every browser session
        rendered empty. The gate must return a reason so the anti-bot downgrade
        (which exempts coverage failures) cannot rewrite target:strategy."""
        from webapp.agents.nodes.route_after_testing import _discovery_coverage_failure

        report = {
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": "empty_render",
                "discovered_urls": 0,
                "found": 0,
            },
        }
        reason = _discovery_coverage_failure(report)
        assert reason and "empty_render" in reason


# ─── N4: a re-drive must not inherit the previous run's test budget ─────────


class TestStaleTestReport:
    def test_parse_command_nulls_test_report(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "parse_command.py"),
            encoding="utf-8",
        ).read()
        assert '"test_report": None' in src, (
            "parse_command must null test_report on fresh runs — the graph "
            "checkpoint otherwise restores the previous run's report and "
            "_invoke_code_writer burns a retry cycle on it (job 83)"
        )

    def test_writer_ignores_a_report_with_no_file(self):
        src = _graph_src()
        assert (
            'state.get("test_report") and not os.path.isfile(' in src
        ), "stale-report belt missing in _invoke_code_writer"
        assert 'state = {**state, "test_report": None}' in src

    def test_retry_bump_still_keys_on_a_real_report(self):
        """The bump itself is unchanged — a REAL prior report still counts a
        retry (within-run cascade semantics preserved)."""
        src = _graph_src()
        assert "if state.get(\"test_report\"):" in src
        assert 'update["test_retry_count"] = current_count + 1' in src


# ─── N5: warm-up must not kill the run on hard nav errors ────────────────────


class TestPlaywrightWarmupContract:
    def _template(self) -> str:
        return open(
            os.path.join(ROOT, "templates", "playwright_scraper.py"), encoding="utf-8"
        ).read()

    def test_warmup_catches_hard_navigation_errors(self):
        src = self._template()
        assert "warm-up goto failed on" in src, (
            "warm-up catch must cover hard errors (net::ERR_INVALID_AUTH_"
            "CREDENTIALS killed whole runs in job 83 — the warm-up goto has no "
            "per-item caller above it)"
        )
        # the widened catch replaced the timeout-only one at the warm-up
        assert src.count("except Exception:") >= 1

    def test_per_item_timeout_contract_preserved(self):
        """job-78's contract is untouched: scrape_product still special-cases
        the never-idle timeout."""
        src = self._template()
        assert "except PlaywrightTimeoutError:" in src
        assert "extracting from the rendered DOM" in src


# ─── N6: the discovery contract is visible in the job log ────────────────────


class TestNavSummaryRow:
    def test_browser_traverse_emits_a_summary_row(self):
        src = _graph_src()
        assert "def _log_event_row(" in src
        assert "[NAV-SUMMARY]" in src
        # the emit sits inside the browser_traverse node, before the analysis
        # is built — after fallback resolution settled result/_disc_fb
        assert src.index("[NAV-SUMMARY]") < src.index('"discovery_method": "browser_traverse"')


# ─── N7: the tester volume bar carries the deterministic scope waiver ───────


class TestTesterVolumeWaiver:
    def _tester_text(self, extra: dict) -> str:
        from webapp.agents.subagents import build_code_tester_message

        state = {
            "site_slug": "s",
            "input_mode": "list_page",
            "url": JOB88_PDP,
            "search_criteria": JOB88_LISTING,
        }
        state.update(extra)
        return "\n".join(str(m.content) for m in build_code_tester_message(state))

    def test_bounded_scope_waives_the_volume_bar(self):
        txt = self._tester_text({"scope": "firstn", "scope_value": "10"})
        assert "BOUNDED SCOPE" in txt

    def test_unbounded_scope_keeps_the_volume_bar(self):
        txt = self._tester_text({})
        assert "BOUNDED SCOPE" not in txt
        assert "pagination/discovery is broken" in txt

    def test_waiver_conditions_match_volume_gap(self):
        """filter-scope or any scope_value must waive, mirroring
        route_after_testing._volume_gap's bail-out."""
        txt = self._tester_text({"scope": "filter"})
        assert "BOUNDED SCOPE" in txt
        txt = self._tester_text({"scope": "", "scope_value": "25"})
        assert "BOUNDED SCOPE" in txt


# ─── N8: RC1 reaches the clean rc=0-no-output run ────────────────────────────


class TestRC1NoFreshOutput:
    def test_no_fresh_output_is_zero_discovery(self):
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            {"execution_status": "FAILED", "no_fresh_output": True, "product_count": 0}
        ) is True

    def test_missing_file_without_flag_is_not_zero_discovery(self):
        """Regression lock: a plain missing output file (crash/timeout path)
        stays non-diagnostic — the flag is what certifies the CLEAN exit."""
        from webapp.agents.nodes.run_execution import _execution_zero_discovery

        assert _execution_zero_discovery(
            {"execution_status": "FAILED", "product_count": 0}
        ) is False

    def test_failed_no_output_result_is_rescued(self, tmp_path):
        """The job-88 end-to-end shape: FAILED + no_fresh_output + the real
        listing only in search_criteria → one bounded retry adopts it."""
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        state = {"url": JOB88_PDP, "navigation_analysis": {},
                 "search_criteria": JOB88_LISTING}
        calls = []

        def redispatch(alt):
            calls.append(alt)
            return {"execution_status": "SUCCESS", "product_count": 8,
                    "output_file": str(tmp_path / "out.json")}

        result = _maybe_retry_execution_listing(
            {"execution_status": "FAILED", "no_fresh_output": True,
             "product_count": 0},
            state, JOB88_PDP, redispatch,
        )
        assert calls == [JOB88_LISTING]
        assert result["product_count"] == 8

    def test_failed_without_flag_still_not_rescued(self):
        """A crash/timeout FAILED result remains the strategy ladder's domain —
        the listing fallback must not fire on it."""
        from webapp.agents.nodes.run_execution import _maybe_retry_execution_listing

        state = {"url": JOB88_PDP, "navigation_analysis": {},
                 "search_criteria": JOB88_LISTING}
        result = {"execution_status": "FAILED",
                  "error_message": "Scraper exited with code 1. boom",
                  "product_count": 0}
        out = _maybe_retry_execution_listing(result, state, JOB88_PDP, lambda alt: {})
        assert out is result


# ─── N9: zero discovery exits non-zero with a marker ─────────────────────────


class TestHttpNavigationZeroExit:
    def _template(self) -> str:
        return open(
            os.path.join(ROOT, "templates", "http_navigation_scraper.py"),
            encoding="utf-8",
        ).read()

    def test_zero_discovery_guard_exits_three(self):
        src = self._template()
        i = src.index("if not discovered_urls and not args.discover_only:")
        block = src[i : i + 1200]
        assert "DISCOVERY_ZERO" in block
        assert "sys.exit(3)" in block
        assert "sys.exit(0)" not in block

    def test_discover_only_contract_untouched(self):
        """--discover-only still writes its (empty) coverage output — the probe
        and the tester's Phase-1 check depend on it."""
        src = self._template()
        assert "args.discover_only:" in src
        assert "empty_first_page" in src


# ─── N12 + partial-draft floor: writer-side deterministic guards ─────────────


class TestWriterDeterministicGuards:
    def test_input_urls_preserve_tolerates_bare_array(self):
        src = _graph_src()
        assert "isinstance(_loaded, dict)" in src
        assert "_loaded if isinstance(_loaded, list) else []" in src
        # the old unconditional .get chain is gone
        assert '_json.load(_ef).get("urls", [])' not in src

    def test_dead_invocation_draft_must_parse(self):
        src = _graph_src()
        assert "_draft_ok" in src
        assert "_ast.parse(_df.read(), filename=_draft_path)" in src
        assert "uncompilable draft" in src

    def test_alive_invocation_path_unchanged(self):
        """The floor keys on a DEAD invocation (_cw_dead) — a healthy
        invocation's draft goes to the LLM self-check loops as before."""
        src = _graph_src()
        assert "if _draft_ok and _cw_dead:" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
