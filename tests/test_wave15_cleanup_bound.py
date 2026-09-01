"""[wave-15 PR-2a] Cleanup is now a bounded LLM phase + dead wrapper removed.

``_invoke_cleanup`` was the last raw ``agent.invoke`` on the happy path: a
hung cleanup LLM call produced no heartbeat and no SessionLog rows, so the
job looked silent to the watchdog while the thread sat in a socket read
(job-133 class, tail side). It now runs under ``_invoke_agent_with_timeout``
with a heartbeat, exactly like skill_learner since QW-1.

Also removed: ``create_agent_with_retry`` (nodes/retry_wrapper.py) — a
superseded retry wrapper with zero live callers; deleting it stops the next
complexity pass from "fixing" a dead path.

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_wave15_cleanup_bound.py -v
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

import pytest  # noqa: E402


def _cleanup_body() -> str:
    src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
    start = src.index("def _invoke_cleanup(")
    end = src.index("def _invoke_skill_learner(")
    return src[start:end]


class TestCleanupBound:
    def test_cleanup_runs_under_the_wall_clock_wrapper(self):
        body = _cleanup_body()
        assert "_invoke_agent_with_timeout(" in body, (
            "cleanup must run under the bounded wrapper, not a bare invoke"
        )
        assert 'agent.invoke(' not in body, (
            "no raw agent.invoke in _invoke_cleanup — the wrapper owns it"
        )

    def test_cleanup_beats_heartbeat_while_it_waits(self):
        body = _cleanup_body()
        assert '_start_heartbeat(job_id, "cleanup")' in body
        assert "_stop_heartbeat(hb)" in body
        # try/finally so a wrapper exception can't leak the heartbeat timer
        assert body.index("hb = _start_heartbeat") < body.index("finally:")
        assert "_stop_heartbeat(hb)" in body.split("finally:")[1]

    def test_cleanup_is_the_last_bare_invoke_removed(self):
        """The ONLY remaining raw agent.invoke CALL in graph.py is the one
        inside the wrapper's daemon thread (comments/docstrings excluded)."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        call_lines = [
            (i, line) for i, line in enumerate(src.splitlines(), 1)
            if ("= agent.invoke(" in line or "return agent.invoke(" in line)
            and not line.lstrip().startswith("#")
        ]
        assert len(call_lines) == 1, (
            f"unexpected raw agent.invoke calls at {[(i, l.strip()) for i, l in call_lines]} "
            "— route through _invoke_agent_with_timeout"
        )
        assert "result_box[0] = agent.invoke(" in call_lines[0][1]


class TestRetryWrapperGone:
    def test_create_agent_with_retry_is_deleted(self):
        assert not os.path.exists(
            os.path.join(ROOT, "webapp", "agents", "nodes", "retry_wrapper.py")
        ), "superseded retry wrapper must stay deleted (zero callers)"
        init = open(os.path.join(ROOT, "webapp", "agents", "nodes", "__init__.py")).read()
        assert "create_agent_with_retry" not in init
        assert "retry_wrapper" not in init

    def test_no_module_still_imports_the_wrapper(self):
        for root, _dirs, files in os.walk(os.path.join(ROOT, "webapp")):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                src = open(path, encoding="utf-8", errors="ignore").read()
                assert "create_agent_with_retry" not in src, path

    def test_scraper_promotion_still_precedes_evidence_archive(self):
        """Body-order pin (test_failure_evidence_archive.py) survives the
        rewrite: promote BEFORE archive, so a FAILED promote can't lose the
        failed cycle's evidence."""
        body = _cleanup_body()
        assert "_promote_scraper(" in body
        assert "_archive_failure_evidence(slug, job_id," in body
        assert body.index("_promote_scraper(") < body.index(
            "_archive_failure_evidence(slug, job_id,"
        )


@pytest.mark.django_db
def test_cleanup_timeout_marks_phase_without_hanging(monkeypatch):
    """Behavior twin: a cleanup invoke that exceeds its wall clock returns
    the dead-invocation marker instead of raising, so the graph keeps its
    honest-failure routing."""
    from types import SimpleNamespace

    import agents.graph as g

    monkeypatch.setattr(g, "build_cleanup_message", lambda state: [])
    monkeypatch.setattr(
        g, "create_cleanup_agent", lambda site_slug="": SimpleNamespace()
    )
    monkeypatch.setattr(
        g, "_archive_existing_scraper", lambda slug: None
    )
    monkeypatch.setattr(
        g,
        "_invoke_agent_with_timeout",
        lambda *a, **k: {"messages": [], "_error": "wall-clock timeout after 1s",
                         "_error_class": "WallClockTimeout"},
    )
    monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
    monkeypatch.setattr(g, "_promote_scraper", lambda *a, **k: None)
    monkeypatch.setattr(g, "_archive_failure_evidence", lambda *a, **k: None)
    monkeypatch.setattr(g, "_start_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(g, "_stop_heartbeat", lambda *a, **k: None)

    out = g._invoke_cleanup({"job_id": 1, "site_slug": "x"}, {})
    assert out == {"messages": []}
