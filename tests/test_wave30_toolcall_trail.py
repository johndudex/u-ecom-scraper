"""[wave-30 W30-6] ToolCallLog: persist the abandoned invocation's real trail.

Prod proof (job 569): ToolCallLog is return-value-derived —
``_persist_agent_logs`` iterates ``result["messages"]``, and a wall-clock
abandonment returns ``{"messages": []}``. So the writer's ~42 REAL tool
calls (SessionLog ``[TOOL]`` rows prove them) logged as ZERO rows in
``/jobs/569/tool-calls/`` — the first forensic RCA literally concluded
"the writer did nothing" from that endpoint. (That verdict was wrong; the
table lied.)

Narrow contract (no dedup keys, no joins):
- ``_ToolCallLogger`` keeps an in-memory per-invocation call list as it
  writes SessionLog rows in real time;
- the abandon site in ``_invoke_agent_with_timeout`` — the one place that
  knows BOTH the truth (the callback's list) and the certainty (the thread
  outlived its window) — persists that list once, marked as an
  abandonment trail;
- healthy invocations never write trail rows (the existing return-value
  path owns them; zero duplicate risk);
- a logger without the trail attribute (test doubles, other handlers) is
  skipped silently.

DB writes are faked at the model boundary (function-local imports resolve
at call time); no database is touched.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402


class FakeManager:
    def __init__(self):
        self.rows = []

    def filter(self, **kw):
        return self

    def count(self):
        return len(self.rows)

    def create(self, **kw):
        self.rows.append(kw)
        return types.SimpleNamespace(**kw)


class FakeModel:
    """Model-shaped double: production code reaches ``Model.objects.*``."""

    ROLE_SYSTEM = "system"
    ROLE_ASSISTANT = "assistant"
    ROLE_TOOL = "tool"
    ROLE_USER = "user"

    def __init__(self):
        self.objects = FakeManager()


@pytest.fixture()
def db_fakes(monkeypatch):
    import scraper.models as sm

    sl, tcl = FakeModel(), FakeModel()
    monkeypatch.setattr(sm, "SessionLog", sl)
    monkeypatch.setattr(sm, "ToolCallLog", tcl)
    return sl.objects, tcl.objects


class TrailAgent:
    """Fires real callback rows, then overruns its window."""

    def __init__(self, logger, seconds=2.0):
        self.logger = logger
        self.seconds = seconds

    def invoke(self, *a, **k):
        self.logger.on_tool_start(
            {"name": "read_file"}, '{"path": "src/http_fetch.py"}'
        )
        self.logger.on_tool_start(
            {"name": "edit_file"}, '{"path": "workspace/s/scraper_draft.py"}'
        )
        time.sleep(self.seconds)
        return {"messages": ["late"]}


class HealthyAgent:
    def __init__(self, logger):
        self.logger = logger

    def invoke(self, *a, **k):
        self.logger.on_tool_start({"name": "check_syntax"}, "{}")
        return {"messages": ["done"]}


def _cfg(logger):
    return {"callbacks": [logger]}


class TestAbandonedTrail:
    def test_abandonment_persists_the_real_calls(self, db_fakes):
        _sl, tcl = db_fakes
        logger = graph._ToolCallLogger(569, "code-writer")
        result = graph._invoke_agent_with_timeout(
            TrailAgent(logger), [], _cfg(logger), "code_tester", 569, timeout=1,
        )
        assert result.get("_error_class") == "WallClockTimeout"
        names = {r.get("tool_name") for r in tcl.rows}
        assert names == {"read_file", "edit_file"}, (
            "the abandoned invocation's real tool calls must reach ToolCallLog "
            "(569: ~42 real calls logged as zero)"
        )
        assert any("src/http_fetch.py" in str(r.get("args_summary")) for r in tcl.rows)
        assert all(
            "abandon" in str(r.get("result_summary", "")).lower() for r in tcl.rows
        ), "trail rows must be marked as abandonment evidence"

    def test_healthy_invocation_writes_no_trail(self, db_fakes):
        _sl, tcl = db_fakes
        logger = graph._ToolCallLogger(569, "code-writer")
        graph._invoke_agent_with_timeout(
            HealthyAgent(logger), [], _cfg(logger), "code_tester", 569, timeout=5,
        )
        assert tcl.rows == [], (
            "healthy invocations keep the return-value persistence path — "
            "the trail must never duplicate it"
        )

    def test_trail_persisted_once_per_logger(self, db_fakes):
        """Two consecutive abandonments sharing one logger (same node, two
        windows) must not double-write the trail."""
        _sl, tcl = db_fakes
        logger = graph._ToolCallLogger(569, "code-writer")
        for _ in range(2):
            graph._invoke_agent_with_timeout(
                TrailAgent(logger), [], _cfg(logger), "code_tester", 569, timeout=1,
            )
        assert len(tcl.rows) == 2

    def test_no_activity_logger_abandon_is_unchanged(self, db_fakes):
        """config without a logger (legacy callers/tests): the abandonment
        behaves exactly as before — empty result, no trail machinery."""
        _sl, tcl = db_fakes
        result = graph._invoke_agent_with_timeout(
            TrailAgent(graph._ToolCallLogger(569, "code-writer")),
            [], {}, "code_tester", 569, timeout=1,
        )
        assert result.get("_error_class") == "WallClockTimeout"
        assert tcl.rows == []

    def test_trail_skipped_without_job_id(self, db_fakes):
        _sl, tcl = db_fakes
        logger = graph._ToolCallLogger(0, "code-writer")
        result = graph._invoke_agent_with_timeout(
            TrailAgent(logger, seconds=2.0), [], _cfg(logger), "code_tester", 0,
            timeout=1,
        )
        assert result.get("_error_class") == "WallClockTimeout"
        assert tcl.rows == []


class TestCallbackCallList:
    def test_logger_collects_calls_in_memory(self, db_fakes):
        logger = graph._ToolCallLogger(569, "code-writer")
        logger.on_tool_start({"name": "read_file"}, '{"path": "a.py"}')
        assert logger.calls == [{"name": "read_file", "args": '{"path": "a.py"}'}]

    def test_serialized_without_name_records_unknown(self, db_fakes):
        logger = graph._ToolCallLogger(569, "code-writer")
        logger.on_tool_start(None, "x")
        assert logger.calls[0]["name"] == "unknown"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
