"""[wave-22 B5] A job that dies without executing must headline its REAL
verdict — not a generic cascade sentence.

Job 338 died after the tester's last report said exactly what was wrong
(FAIL, real issues), but the job row's headline was the generic "Pipeline
ended before execution (testing cascade exhausted without a passing run)" —
``_diagnose_no_execution`` found the report, saw it was not PASS, and fell
through to the canned message WITHOUT quoting it. The morning RCA then had
to re-open artifacts to learn what the pipeline already knew.

Contract:
- a non-PASS report → the diagnosis carries the report's actual assessment,
  confidence, and its first HIGH-severity issue message;
- a PASS report → the existing "execution never ran (interrupt/resume gap)"
  message is unchanged;
- no readable report → the generic fallback is unchanged;
- the final catch-all route arm logs its cascade decision (the last
  ``return "human_approval"`` of route_after_testing).
"""
from __future__ import annotations

import importlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

import src.artifacts as artifacts  # noqa: E402

tasks = importlib.import_module("scraper.tasks")  # noqa: E402


@pytest.fixture()
def report_env(tmp_path, monkeypatch):
    """Workspace with PROJECT_ROOT pointed at tmp; FM artifacts mocked OFF so
    the workspace copy is the one read (deterministic, no HTTP).
    ``raising=False``: leaky tests elsewhere in the suite delattr the exists
    helper, and the patch must survive that ordering."""
    ws = tmp_path / "workspace" / "example-com"
    ws.mkdir(parents=True)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(artifacts, "exists", lambda key: False, raising=False)
    return ws


def _write_report(ws, report: dict) -> None:
    (ws / "test_report.json").write_text(json.dumps(report))


FAIL_REPORT = {
    "overall_assessment": "FAIL",
    "confidence_score": 0.97,
    "phase2_confidence": 0.97,
    "issues": [
        {"severity": "low", "message": "cosmetic"},
        {
            "severity": "high",
            "message": "price extraction returns empty on 21/21 sampled items",
            "description": "price extraction returns empty on 21/21 sampled items",
        },
    ],
}


class TestDiagnoseQuotesTheVerdict:
    def test_fail_report_names_verdict_and_first_high_issue(
        self, report_env, monkeypatch
    ):
        _write_report(report_env, FAIL_REPORT)
        msg = tasks._diagnose_no_execution("example-com", 338)
        assert "FAIL" in msg, msg
        assert "0.97" in msg, "diagnosis must carry the report's confidence"
        assert "price extraction returns empty" in msg, (
            "diagnosis must quote the report's first HIGH-severity issue — "
            "the pipeline already knew what was wrong (338)"
        )

    def test_fail_report_without_issues_still_names_verdict(
        self, report_env
    ):
        _write_report(
            report_env,
            {"overall_assessment": "FAIL", "confidence_score": 0.4, "issues": []},
        )
        msg = tasks._diagnose_no_execution("example-com", 1)
        assert "FAIL" in msg and "0.40" in msg

    def test_prefers_high_severity_over_low(self, report_env):
        _write_report(report_env, FAIL_REPORT)
        msg = tasks._diagnose_no_execution("example-com", 338)
        assert "cosmetic" not in msg


class TestExistingSemanticsUnchanged:
    def test_pass_report_keeps_interrupt_gap_message(self, report_env):
        _write_report(
            report_env,
            {"overall_assessment": "PASS", "confidence_score": 0.9},
        )
        msg = tasks._diagnose_no_execution("example-com", 74)
        assert "interrupt/resume gap" in msg

    def test_no_report_keeps_generic_fallback(self, report_env):
        msg = tasks._diagnose_no_execution("example-com", 1)
        assert "Pipeline ended before execution" in msg

    def test_message_is_bounded(self, report_env):
        long_issue = {"severity": "high", "message": "x" * 5000}
        _write_report(
            report_env,
            {"overall_assessment": "FAIL", "issues": [long_issue]},
        )
        assert len(tasks._diagnose_no_execution("example-com", 1)) <= 2000


class TestTerminalRouteLogging:
    def test_final_catchall_arm_logs_cascade(self):
        with open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py")
        ) as fh:
            src = fh.read()
        i_tail = src.rindex('_terminal_after_grace_check(state, "human_approval")')
        block = src[max(0, i_tail - 600): i_tail]
        assert "_log_cascade" in block, (
            "the final catch-all route arm returns without logging its "
            "cascade decision — the one arm RCAs could never see"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
