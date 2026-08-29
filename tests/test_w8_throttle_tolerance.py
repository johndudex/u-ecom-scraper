"""W8: 429 throttled-classification + tester-path retry tolerance.

Prod shape (browser-service under memory pressure): /navigate returns 429
with a Retry-After while NAVIGATE is saturated. Before W8 the generated
http_navigation scraper treated 429-exhaustion exactly like a hard failure
(``navigate_error`` → coverage FAIL → strategy switch), and every webapp
/scrape caller turned a 429/502 body into empty output or an opaque
HTTPStatusError. This pins the tolerant contract:

- template: 429-exhaustion returns a terminal ``throttled`` dict; discovery
  emits ``stop_reason="navigate_throttled"`` (INCONCLUSIVE, priority 3);
  Retry-After is read header-first then body.
- classifier: a throttled run is UNPROVEN coverage — re-test the same draft
  ("scraper"), never a strategy switch, never "refine", never coverage-FAIL.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_w8_throttle_tolerance.py -q
"""
from __future__ import annotations

import os
import pathlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "templates", "http_navigation_scraper.py")


def _template_src() -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


class TestTemplateThrottleContract:
    def test_stop_reason_priority_places_throttled_inconclusive(self):
        src = _template_src()
        assert '"navigate_throttled": 3' in src, (
            "429 backpressure is INCONCLUSIVE (same band as max_pages_hit), "
            "NOT a coverage FAIL"
        )
        assert '"navigate_error": 5' in src, (
            "hard navigate failures (502/503/block) must stay FAIL priority 5"
        )
        # Inconclusive must rank BELOW the FAIL band in the map body.
        i_thr = src.index('"navigate_throttled": 3')
        i_err = src.index('"navigate_error": 5')
        assert i_err < i_thr

    def test_navigate_returns_terminal_throttled_dict_on_429_exhaustion(self):
        src = _template_src()
        assert "last_throttled" in src
        assert (
            '{"success": False, "throttled": True, "status": 429, "url": url, "html": ""}'
            in src
        ), "429-exhaustion must return the terminal dict, not bare None"

    def test_retry_after_read_header_first_then_body(self):
        src = _template_src()
        i_header = src.index('r.headers.get("Retry-After")')
        i_body = src.index('(r.json() or {}).get("retry_after")')
        assert i_header < i_body, (
            "server emits the header post-W4; older builds only the body — "
            "header must be consulted first"
        )

    def test_all_four_discovery_sites_emit_navigate_throttled(self):
        """search first-page + search pagination + category first-page +
        category pagination: every discovery break/return must distinguish
        throttled from navigate_error."""
        src = _template_src()
        # first-page returns use the `throttled` local…
        assert src.count('return [], "navigate_throttled" if throttled else "navigate_error"') == 2
        # …pagination breaks read it off the response dict.
        assert src.count('"navigate_throttled" if (resp and resp.get("throttled"))') == 2


class TestClassifierThrottledBranch:
    def _report(self, stop_reason="navigate_throttled", items=0):
        return {
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": stop_reason,
                "discovered_urls": items,
            },
        }

    def test_throttled_retests_same_draft(self):
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        action, reason = classify_test_failure(self._report(), "http_navigation")
        assert action == "scraper", "a throttle never got a fair window — re-test"
        assert "throttled" in reason.lower()

    def test_throttled_outranks_http_zero_item_strategy_switch(self):
        """The :139-140 `items == 0 and is_http_like` branch fires before the
        coverage gate — the throttled branch MUST precede it (job-311's
        transient shape, now for backpressure)."""
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        action, _ = classify_test_failure(self._report(), "http_navigation")
        assert action == "scraper"

    def test_throttled_is_not_a_coverage_fail(self):
        """_COVERAGE_FAIL_STOP_REASONS must exclude navigate_throttled — a
        throttled run is unproven, not a give-up."""
        from webapp.agents.nodes.route_after_testing import (
            _COVERAGE_FAIL_STOP_REASONS,
            _discovery_coverage_failure,
        )

        assert "navigate_throttled" not in _COVERAGE_FAIL_STOP_REASONS
        assert _discovery_coverage_failure(self._report()) is None
        assert (
            _discovery_coverage_failure(self._report("navigate_error")) is not None
        ), "hard navigate failures stay coverage-FAIL"

    def test_selector_crash_still_wins(self):
        """Ordering is deliberate: an unambiguous selector bug is actionable
        now; the throttle signal is not a reason to skip fixing the code."""
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        report = self._report()
        report["crash_error"] = "PlaywrightError: failed to find element .buy-now"
        action, reason = classify_test_failure(report, "http_navigation")
        assert action == "scraper"
        assert "selector" in reason.lower()

    def test_navigate_error_still_switches_strategy(self):
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        # items > 0 (a partial run) routes through the coverage-FAIL band:
        # discovery GAVE UP mid-run — not a field-quality "refine".
        report = self._report("navigate_error", items=10)
        report["successful_extractions"] = 10
        action, reason = classify_test_failure(report, "http_navigation")
        assert action == "strategy", (
            "502/503/block after retries IS a strategy-class failure"
        )
        assert "navigate_error" in reason


class TestCallSiteAdoption:
    """All five webapp /scrape callers must go through the shared helper —
    one bare raise_for_status() reintroduces the backpressure blind spot."""

    def _src(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_no_bare_scrape_posts_remain_in_webapp(self):

        # graph.py:3919 is the plan's documented EXCLUSION: its raise sits
        # inside try/except → (False, None) inconclusive-instant; adopting the
        # helper would turn that into "inconclusive, ~13 minutes later".
        excluded = {"agents/graph.py"}
        unadopted = []
        for p in pathlib.Path(ROOT, "webapp").rglob("*.py"):
            rel = p.relative_to(os.path.join(ROOT, "webapp")).as_posix()
            text = p.read_text(encoding="utf-8", errors="ignore")
            if (
                '/scrape"' in text
                and "browser_http" not in text
                and not p.name.startswith(("test", "__head__"))
                and rel not in excluded
            ):
                unadopted.append(rel)
        assert unadopted == [], f"/scrape callers not on the shared helper: {unadopted}"

    def test_helper_retries_transient_band_only(self):
        src = self._src("webapp/agents/tools/browser_http.py")
        assert "RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})" in src
        # 404 must be checked BEFORE the retryable band (never retried).
        assert src.index("HTTP_NOT_FOUND = 404") < src.index(
            "if resp.status_code in RETRYABLE_STATUS_CODES"
        )

    def test_rerun_button_scrape_timeout_is_capped(self):
        src = self._src("webapp/scraper/views.py")
        assert "min(int(scrape_json.get(\"timeout\") or 0), 1200)" in src, (
            "a 3600s synchronous HTTP hold occupies a gunicorn worker (and a "
            "post-W4 SCRAPE slot) for an hour — capped at 1200s"
        )


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
