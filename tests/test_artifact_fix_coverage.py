"""Coverage extension for the artifact-corruption net (plan Piece 2).

The repair-on-write hook existed ONLY for product_analyzer. site_analyzer's
site_analysis.json and code_tester's test_report.json were both unguarded, and
code_tester does not even go through _run_budgeted_agent (invoked directly), so
the one guard in the codebase could not apply to it. A corrupt test_report made
_load_test_report return None → route_after_testing treated it as "no report" →
the retry loop burned budget on a phantom failure (the priceline instance).

Fixes under test:
  * site_analyzer passes artifact_fix_fn → corrupt site_analysis.json is
    repaired (not just "missing").
  * _invoke_code_tester runs the repair ONCE before concluding the report is
    missing, then reloads.
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

import agents.graph as g  # noqa: E402


# ─── site_analyzer gets the artifact_fix_fn hook ─────────────────────────────

class _StubAgent:
    """Stands in for the create_react_agent product — invoked once, no tools."""

    def __init__(self, result=None):
        self._result = result or {"messages": []}

    def invoke(self, messages, config=None):
        return self._result


class TestSiteAnalyzerArtifactFix:
    def _run(self, tmp_path, monkeypatch, corrupt_body):
        slug = "s"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True, exist_ok=True)
        artifact = ws / "site_analysis.json"
        artifact.write_text(corrupt_body, encoding="utf-8")

        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(g, "build_site_analyzer_message", lambda state: [])
        monkeypatch.setattr(
            g, "create_site_analyzer", lambda site_slug="": _StubAgent()
        )
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_start_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(g, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        monkeypatch.setattr(
            g, "_invoke_agent_with_timeout",
            lambda agent, messages, cfg, phase, job_id: agent.invoke(messages),
        )
        return artifact

    def test_corrupt_site_analysis_repaired_not_missing(self, tmp_path, monkeypatch):
        """Control-char corruption (the priceline class) in site_analysis.json
        must be repaired by the phase-exit hook instead of leaving the artifact
        unparseable (→ {} → downstream runs on empty platform data)."""
        corrupt = '{"platform": "sfcc", "note": "a' + chr(10) + 'b"}'
        artifact = self._run(tmp_path, monkeypatch, corrupt)
        from langgraph.types import RunnableConfig

        out = g._invoke_site_analyzer(
            {"job_id": 0, "site_slug": "s", "input_mode": "url_list"},
            RunnableConfig(),
        )
        # the artifact survived (not renamed .corrupt) and now parses
        assert artifact.is_file(), "site_analysis.json was quarantined instead of repaired"
        data = json.loads(artifact.read_text())
        assert data["platform"] == "sfcc"
        # the state update carried the repaired analysis
        upd = out.update if hasattr(out, "update") else out
        assert upd["site_analysis"]["platform"] == "sfcc"


# ─── code_tester: repair-then-reload once before "no report" ────────────────

class TestCodeTesterRepairThenReload:
    def _run(self, tmp_path, monkeypatch, body, retry_count=0):
        slug = "s"
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True, exist_ok=True)
        artifact = ws / "test_report.json"
        artifact.write_text(body, encoding="utf-8")
        # [job-81 N-C] the repair gate only fires for a file written DURING
        # the attempt (mtime >= the node's entry stamp) — the LLM writes the
        # report mid-invoke, after the stamp. Stamp the fixture accordingly.
        _during_attempt = time.time() + 120
        os.utime(artifact, (_during_attempt, _during_attempt))

        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(g, "build_code_tester_message", lambda state: [])
        monkeypatch.setattr(g, "create_code_tester", lambda site_slug="": _StubAgent())
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "_attach_discovery_coverage", lambda rep, slug: rep)
        monkeypatch.setattr(g, "_preserve_test_report", lambda slug: None)

        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path))
        return ws / "test_report.json"

    def test_corrupt_report_repaired_before_missing_conclusion(self, tmp_path, monkeypatch):
        """A corrupt test_report.json must be repaired and RELOADED before
        _invoke_code_tester concludes there is no report (which drives the
        retry loop / F19 failed-finalize path)."""
        corrupt = (
            '{"overall_assessment": "PASS",\n'
            ' "feedback_for_writer": "phase 2 fixed' + chr(10) + chr(10) + 'phase 1 broken"}'
        )
        artifact = self._run(tmp_path, monkeypatch, corrupt)
        from langgraph.types import RunnableConfig

        out = g._invoke_code_tester(
            {"job_id": 0, "site_slug": "s", "test_retry_count": 0},
            RunnableConfig(),
        )
        assert json.loads(artifact.read_text())["overall_assessment"] == "PASS"
        assert out.get("test_report", {}).get("overall_assessment") == "PASS"

    def test_repair_attempted_exactly_once_when_corrupt(self, tmp_path, monkeypatch):
        """The repair-then-reload is a single attempt (repair → reload → move
        on), not a loop; and it is not attempted when there is no file."""
        calls = []

        def counting_fix(slug, filename):
            calls.append((slug, filename))
            return None

        self._run(tmp_path, monkeypatch, '{"a": "x' + chr(10) + 'y"}')
        monkeypatch.setattr(g, "_fix_json_artifact", counting_fix)
        from langgraph.types import RunnableConfig

        g._invoke_code_tester(
            {"job_id": 0, "site_slug": "s", "test_retry_count": 0},
            RunnableConfig(),
        )
        assert calls == [("s", "test_report.json")], (
            "expected exactly one repair attempt on the corrupt report"
        )

        # no file at all → no repair attempt
        calls.clear()
        (tmp_path / "workspace" / "s" / "test_report.json").unlink()
        g._invoke_code_tester(
            {"job_id": 0, "site_slug": "s", "test_retry_count": 0},
            RunnableConfig(),
        )
        assert calls == []
