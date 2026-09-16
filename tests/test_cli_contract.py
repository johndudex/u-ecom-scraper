"""Guard-side tests for the CLI-contract enforcement (docs/cli-contract-plan.md v2).

Covers: cli_contract_violation (M0-M4 incl. the api M2-demotion regression),
template-family compliance, route_after_testing's _contract_bad branches, and
the L3 honesty floor. No network, no DB.

Run: docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
     celery-worker bash -c "cd /app && python -m pytest tests/test_cli_contract.py -v"
"""

from __future__ import annotations

import os
import sys
import textwrap
import types
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import importlib as _importlib

# `agents.nodes` re-exports a FUNCTION named run_execution (the node itself),
# which shadows the same-named submodule on attribute access. Fetch the module
# object through sys.modules explicitly.
import agents.nodes.run_execution as _rx_mod  # noqa: F401 (registers in sys.modules)
rx = _importlib.import_module("agents.nodes.run_execution")
assert hasattr(rx, "cli_contract_violation"), "module import resolved to the node function"


# ── Draft fixtures ───────────────────────────────────────────────────────────

def _write_draft(tmp_path, src: str) -> str:
    p = tmp_path / "scraper_draft.py"
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return str(p)


JOB7_SHAPE = """
    import argparse
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", type=str, default=None)
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        parser.add_argument("--urls", nargs="+", default=None)
        args = parser.parse_args()
        if args.query:
            discover(args.query)
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""

ENV_GATE_SHAPE = """
    import argparse, os
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--listing-url", type=str, default=None)
        parser.add_argument("--fresh-discovery", action="store_true")
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
        if _env_listing or args.fresh_discovery or args.listing_url:
            discover(_env_listing or args.listing_url)
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""

API_ENVONLY_SHAPE = """
    import argparse, os
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        _env_force = os.environ.get("SCRAPER_FORCE_DISCOVERY", "").strip()
        if args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""

API_FLAG_SHAPE = """
    import argparse, os
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--fresh-discovery", action="store_true")
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        _env_force = os.environ.get("SCRAPER_FORCE_DISCOVERY", "").strip()
        _force = args.fresh_discovery or bool(_env_force)
        if _force:
            paginate_api()
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""

COMMENT_ONLY_SHAPE = """
    import argparse
    # os.environ.get("SCRAPER_LISTING_URL", "") is read by the template
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--query", type=str, default=None)
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        if args.query:
            discover(args.query)
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
"""


class TestChecker:
    def test_url_list_exempt(self, tmp_path):
        p = _write_draft(tmp_path, JOB7_SHAPE)
        assert rx.cli_contract_violation(p, "url_list") is None

    def test_job7_shape_violates_navigation(self, tmp_path):
        p = _write_draft(tmp_path, JOB7_SHAPE)
        v = rx.cli_contract_violation(p, "navigation")
        assert v is not None and "CLI CONTRACT VIOLATION" in v
        assert "--listing-url" in v or "SCRAPER_LISTING_URL" in v

    def test_job7_shape_violates_list_page(self, tmp_path):
        p = _write_draft(tmp_path, JOB7_SHAPE)
        assert rx.cli_contract_violation(p, "list_page") is not None

    def test_query_satisfies_search_term_only(self, tmp_path):
        p = _write_draft(tmp_path, JOB7_SHAPE)
        assert rx.cli_contract_violation(p, "search_term") is None

    def test_env_gate_satisfies(self, tmp_path):
        p = _write_draft(tmp_path, ENV_GATE_SHAPE)
        assert rx.cli_contract_violation(p, "list_page") is None
        assert rx.cli_contract_violation(p, "navigation") is None

    def test_api_envonly_still_violates(self, tmp_path):
        """Critique v1 vector 1A regression: SCRAPER_FORCE_DISCOVERY is never
        SET anywhere — reading it must NOT satisfy the api family."""
        p = _write_draft(tmp_path, API_ENVONLY_SHAPE)
        v = rx.cli_contract_violation(p, "navigation", "internal_api")
        assert v is not None, "M2 must be demoted: env-read-only api draft must violate"
        assert "fresh-discovery" in v

    def test_api_flag_shape_satisfies(self, tmp_path):
        p = _write_draft(tmp_path, API_FLAG_SHAPE)
        assert rx.cli_contract_violation(p, "navigation", "api") is None

    def test_comment_mention_does_not_satisfy(self, tmp_path):
        p = _write_draft(tmp_path, COMMENT_ONLY_SHAPE)
        assert rx.cli_contract_violation(p, "list_page") is not None

    def test_unparseable_returns_none(self, tmp_path):
        p = tmp_path / "scraper_draft.py"
        p.write_text("def broken(:\n", encoding="utf-8")
        assert rx.cli_contract_violation(str(p), "navigation") is None

    def test_missing_file_returns_none(self, tmp_path):
        assert rx.cli_contract_violation(str(tmp_path / "nope.py"), "navigation") is None


class TestTemplateCompliance:
    """All template families must pass their OWN contract (false-positive suite
    — the aya/locumtenens/lw.com classes)."""

    def _tpl(self, name):
        return os.path.join(ROOT, "templates", name)

    def test_playwright_family(self):
        for im in ("navigation", "list_page", "search_term"):
            assert rx.cli_contract_violation(self._tpl("playwright_scraper.py"), im) is None

    def test_http_navigation_family(self):
        for name in ("http_navigation_scraper.py", "navigation_scraper.py"):
            for im in ("navigation", "list_page", "search_term"):
                assert rx.cli_contract_violation(self._tpl(name), im) is None

    def test_requests(self):
        assert rx.cli_contract_violation(self._tpl("requests_scraper.py"), "list_page") is None

    def test_api_family(self):
        for im in ("navigation", "list_page", "search_term"):
            assert rx.cli_contract_violation(self._tpl("api_scraper.py"), im, "internal_api") is None

    def test_ssr(self):
        assert rx.cli_contract_violation(self._tpl("ssr_div_list_scraper.py"), "list_page") is None

    def test_uc_violates_and_that_is_correct(self):
        """UC defines INPUT_FILE (line 64) but wires NO discovery trigger
        (no env gate, no consuming flag) — on a nav job it genuinely cannot
        discover, so the checker flags it. That is correct-by-design: the
        critique's 'UC passes M0 for the right reason' claim was wrong (it
        assumed no INPUT_FILE existed). If UC is ever selected for nav jobs,
        its template needs the gate added — the bounce is the fix prompt."""
        v = rx.cli_contract_violation(
            self._tpl("undetected_chromedriver_scraper.py"), "list_page"
        )
        assert v is not None and "CLI CONTRACT VIOLATION" in v


class TestRouteAfterTesting:
    """L2 routing: _contract_bad blocks the PASS exit and the ground-truth
    override; bounded bounce; exhausted arms."""

    def _route(self, state_updates: dict, tmp_path, draft_src: str):
        # workspace draft for the static re-check
        slug = "contracttest"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text(textwrap.dedent(draft_src), encoding="utf-8")
        base = {
            "site_slug": slug,
            "input_mode": "list_page",
            "test_report": {"overall_assessment": "PASS", "confidence_score": 0.9,
                            "issues": [], "ready_for_execution": True},
            "test_retry_count": 0,
            "scraping_method": "http_navigation",
            "probe_result": {},
        }
        base.update(state_updates)
        rat = _importlib.import_module("agents.nodes.route_after_testing")

        with mock.patch.dict(os.environ, {"PROJECT_ROOT": str(tmp_path)}):
            return rat.route_after_testing(base)

    def test_pass_blocked_by_contract_violation(self, tmp_path):
        assert self._route({}, tmp_path, JOB7_SHAPE) == "code_writer"

    def test_compliant_passes(self, tmp_path):
        # A PASS also needs phases_tested.phase1_discovery=True (the pre-existing
        # phase-coverage gate) — without it the route legitimately sends the job
        # to scraper_analyzer for a discovery-validating re-test.
        # [wave-13] A PASS now also needs a nonzero extracted count — a verdict
        # with zero items behind it is the phantom-PASS class the campaign
        # killed (job-76/81), so the compliant shape carries real evidence.
        state = {
            # [wave-34 F4] a browser-family strategy: the ladder gate is
            # deliberately exempt for it, so the compliant shape tests the
            # routing (not the ladder). An HTTP-family strategy-less draft
            # would now be contract-bad — the tester gate would force-FAIL
            # exactly the same shape (623 class).
            "scraper_analysis": {"strategy": "playwright"},
            "test_report": {
                "overall_assessment": "PASS",
                "confidence_score": 0.9,
                "ready_for_execution": True,
                "issues": [],
                "results": {"successful_extractions": 7},
                "phases_tested": {"phase1_discovery": True, "phase2_extraction": True},
            },
        }
        assert self._route(state, tmp_path, ENV_GATE_SHAPE) == "field_confirmation"

    def test_exhausted_to_human_approval(self, tmp_path):
        assert self._route({"test_retry_count": 2}, tmp_path, JOB7_SHAPE) == "human_approval"

    def test_exhausted_skip_approvals_to_cleanup(self, tmp_path):
        assert (
            self._route({"test_retry_count": 2, "skip_approvals": True}, tmp_path, JOB7_SHAPE)
            == "cleanup"
        )


LADDER_STRIP_SHAPE = """
    import argparse, os
    import requests
    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("--listing-url", type=str, default=None)
        parser.add_argument("--fresh-discovery", action="store_true")
        parser.add_argument("--sample", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--input", type=str, default=None)
        args = parser.parse_args()
        _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
        if _env_listing or args.fresh_discovery or args.listing_url:
            discover(_env_listing or args.listing_url)
        elif args.input:
            load(args.input)
        elif os.path.exists(INPUT_FILE):
            load(INPUT_FILE)
    def discover(listing_url):
        resp = requests.get(listing_url, timeout=30)
        return resp.text
"""


class TestLadderGateRouting(TestRouteAfterTesting):
    """[wave-34 F4] prod 623: the tester forced FAIL on
    ladder_preservation_violation, but _contract_bad only re-derived the CLI
    gate — the ground-truth override rescued the job and run_execution refused
    it, zero fix cycles. The ladder violation must route to code_writer."""

    def _pass_state(self):
        return {
            "scraper_analysis": {"strategy": "http_requests"},
            "test_report": {
                "overall_assessment": "PASS",
                "confidence_score": 0.9,
                "ready_for_execution": True,
                "issues": [],
                "results": {"successful_extractions": 50},
                "phases_tested": {"phase1_discovery": True, "phase2_extraction": True},
            },
        }

    def test_ladder_violation_blocks_ground_truth_override(self, tmp_path):
        state = self._pass_state()
        assert self._route(state, tmp_path, LADDER_STRIP_SHAPE) == "code_writer"

    def test_deterministic_gate_stamp_belt(self, tmp_path):
        # Even when the static re-derivation can't see it, the tester's own
        # stamp (report["deterministic_gate"]) forces the bounce.
        state = self._pass_state()
        state["test_report"]["deterministic_gate"] = "ladder"
        assert self._route(state, tmp_path, ENV_GATE_SHAPE) == "code_writer"

    def test_access_wall_rescue_honors_contract_gate(self, tmp_path):
        # [wave-34 critique leak #1] the ×2 access-wall terminal (and its
        # ground-truth rescue) must not swallow a deterministic contract
        # violation — the ladder class tests green by definition, so riding
        # the wall arms would reach run_execution's refusal with zero fix
        # cycles (623 again, one wall cycle later). Contract bounce wins.
        state = self._pass_state()
        state["access_wall_cycles"] = 2
        assert self._route(state, tmp_path, LADDER_STRIP_SHAPE) == "code_writer"
        # Contract-clean + real items: the W24-3 ground-truth rescue still
        # beats the wall exactly as before (F4 must not touch that lane).
        clean = {
            "scraper_analysis": {"strategy": "playwright"},
            "test_report": {
                "overall_assessment": "PASS",
                "confidence_score": 0.9,
                "ready_for_execution": True,
                "issues": [],
                "results": {"successful_extractions": 7},
                "phases_tested": {"phase1_discovery": True, "phase2_extraction": True},
            },
            "access_wall_cycles": 2,
        }
        assert self._route(clean, tmp_path, ENV_GATE_SHAPE) == "field_confirmation"

    def test_ladder_compliant_draft_still_passes(self, tmp_path):
        state = self._pass_state()
        compliant = LADDER_STRIP_SHAPE.replace(
            "import requests",
            "import requests\n    from src.http_fetch import create_fetch_page",
        )
        state["test_report"] = dict(
            state["test_report"],
            phases_tested={"phase1_discovery": True, "phase2_extraction": True},
        )
        assert self._route(state, tmp_path, compliant) == "field_confirmation"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
