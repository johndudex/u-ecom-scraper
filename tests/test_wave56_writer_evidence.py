"""[wave-56 F1+F3] The writer's retry slot carries the real evidence.

The 10-site capability batch (jobs 570-579, 2026-10-09) produced 6 no-op fix
loop deaths. Forensics on the archived test reports showed the writer was
starved, not stupid — four deterministic starvations:

- ``issues[].details`` (the actual "HTTP 406 ... vanilla urllib3" lines, the
  harness kill notices) never reached the retry prompt — only the issue
  headline and a file pointer the model doesn't act on.
- ``results.failed_extractions`` / ``skipped_dead_urls`` counts and the
  ``discovery_coverage`` fix-execution proof (briscoes 572: cycle 2's own
  report proved ``soft_block_escalations=0`` — the prescribed fixes never
  executed) were not relayed either.
- ``_failure_region_block`` declined the MOST COMMON crash class outright
  (``remediation.target == "scraper"`` → ``return ""``), so the AST preseed
  never pointed the writer at the function named in the traceback.
- On a PASS-then-execution-zero bounce (nordstrom 571) the writer saw a PASS
  summary and had nothing actionable — no line told it that its previous
  draft was the thing under trial and that a byte-identical resubmission
  ends the job.

F3 rides the same slot: when the probe proved a browser-tier method reached
the site but every fetch in the tested draft was rejected at the HTTP/TLS
layer (wonderbly 570's all-406 wall), a selector edit cannot help — the
directive must say so explicitly.
"""
from __future__ import annotations

import os
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


def _report(**over):
    report = {
        "overall_assessment": "FAIL",
        "confidence_score": 0.2,
        "results": {"successful_extractions": 0, "failed_extractions": 3},
        "issues": [
            {
                "severity": "high",
                "field": "discovery",
                "problem": "Every fetch was rejected",
                "details": "GET /catalogue/ -> HTTP 406 Not Acceptable "
                           "(vanilla urllib3/requests TLS client fingerprint)",
                "suggested_fix": "Switch the fetch layer to the browser tier",
            },
        ],
        "remediation": {"target": "scraper", "fields": [], "reason": "all fetches 406"},
    }
    report.update(over)
    return report


def _state(report, **over):
    state = {
        "site_slug": "test-site",
        "job_id": 1,
        "test_report": report,
        "test_retry_count": 1,
        "last_tested_draft_fp": "deadbeefcafe1234567890123456789012345678",
    }
    state.update(over)
    return state


class TestIssueDetailsInlined:
    def test_details_evidence_reaches_retry_summary(self):
        from agents.subagents import _summarize_test_report

        out = _summarize_test_report(_state(_report()))
        # The 406 line lives ONLY in issues[0].details — the headline says
        # "Every fetch was rejected". The writer must see the actual evidence.
        assert "HTTP 406" in out

    def test_details_absent_no_placeholder_noise(self):
        from agents.subagents import _summarize_test_report

        report = _report()
        report["issues"][0].pop("details")
        out = _summarize_test_report(_state(report))
        assert "HTTP 406" not in out


class TestExtractionCountsRelayed:
    def test_failed_and_skipped_counts_relayed(self):
        from agents.subagents import _summarize_test_report

        report = _report(
            results={
                "successful_extractions": 1,
                "failed_extractions": 35,
                "skipped_dead_urls": 2,
            }
        )
        out = _summarize_test_report(_state(report))
        assert "35" in out
        assert "2" in out


class TestFixExecutionProof:
    def test_unexecuted_fixes_called_out(self):
        from agents.subagents import _summarize_test_report

        report = _report(
            discovery_coverage={
                "discovered_urls": 0,
                "stop_reason": "graphql_unavailable",
                "soft_block_escalations": 0,
                "retried_empty_discovery": False,
                "jsonld_fallback_pages": 0,
            }
        )
        out = _summarize_test_report(_state(report))
        assert "NOT executed" in out
        assert "soft_block_escalations=0" in out

    def test_executed_fixes_stated_without_accusation(self):
        from agents.subagents import _summarize_test_report

        report = _report(
            discovery_coverage={
                "discovered_urls": 4,
                "stop_reason": "max_pages_hit",
                "soft_block_escalations": 2,
                "retried_empty_discovery": True,
            }
        )
        out = _summarize_test_report(_state(report))
        assert "NOT executed" not in out
        assert "soft_block_escalations=2" in out


class TestDifferentiationDirective:
    def test_directive_names_prior_draft_fingerprint(self):
        from agents.subagents import _summarize_test_report

        out = _summarize_test_report(_state(_report()))
        assert "DIFFERENTIATION REQUIRED" in out
        assert "deadbeefcafe" in out

    def test_no_directive_on_first_attempt(self):
        from agents.subagents import _summarize_test_report

        out = _summarize_test_report(_state(_report(), test_retry_count=0))
        assert "DIFFERENTIATION REQUIRED" not in out


class TestTransportWallDirective:
    """[F3] Probe proved a browser tier; the draft's plain HTTP got walled."""

    def test_browser_probe_plus_406_wall_names_transport(self):
        from agents.subagents import _summarize_test_report

        state = _state(
            _report(),
            probe_result={
                "connectivity": {"method_that_worked": "browser_none"}
            },
        )
        out = _summarize_test_report(state)
        assert "TRANSPORT WALL" in out
        assert "browser_none" in out

    def test_direct_probe_no_mismatch_directive(self):
        from agents.subagents import _summarize_test_report

        state = _state(
            _report(),
            probe_result={
                "connectivity": {"method_that_worked": "direct_http"}
            },
        )
        out = _summarize_test_report(state)
        assert "TRANSPORT WALL" not in out

    def test_items_extracted_no_wall_directive(self):
        from agents.subagents import _summarize_test_report

        report = _report(
            results={"successful_extractions": 12, "failed_extractions": 0}
        )
        state = _state(
            report,
            probe_result={
                "connectivity": {"method_that_worked": "browser_residential"}
            },
        )
        out = _summarize_test_report(state)
        assert "TRANSPORT WALL" not in out


class TestPreseedCoversScraperCrash:
    """[F1b] ``target == "scraper"`` must not opt out of the AST preseed."""

    @pytest.fixture
    def draft(self, tmp_path):
        p = tmp_path / "scraper_draft.py"
        p.write_text(
            textwrap.dedent(
                """
                import json


                def fetch_listing(url):
                    return "<html></html>"


                def parse_product(html):
                    data = json.loads(html)
                    return {"title": data["title"]}


                def main():
                    parse_product("not-json")
                """
            ),
            encoding="utf-8",
        )
        return str(p)

    def test_traceback_function_region_selected(self, draft):
        from agents.graph import _failure_region_block

        report = _report()
        report["crash_error"] = (
            'Traceback (most recent call last):\n'
            '  File "scraper_draft.py", line 14, in main\n'
            '    parse_product("not-json")\n'
            '  File "scraper_draft.py", line 10, in parse_product\n'
            '    return {"title": data["title"]}\n'
            "json.decoder.JSONDecodeError: Expecting value"
        )
        out = _failure_region_block(_state(report), draft)
        assert out, "scraper-target crash must produce a failure-region preseed"
        assert "parse_product" in out

    def test_crash_without_traceback_frames_stays_empty(self, draft):
        from agents.graph import _failure_region_block

        report = _report()
        report["crash_error"] = "harness killed the run at 420s (discovery wall)"
        out = _failure_region_block(_state(report), draft)
        assert out == ""
