"""[wave-22 A3] A task-scoped job budget must subordinate every phase wall
clock to the celery soft limit.

Before wave-22 the system had NO job-level deadline: each phase got the same
flat ``_AGENT_INVOKE_TIMEOUT`` regardless of how much of the task's 3h+ had
already burned, so a job that spent 2.5h in early phases still started a full
900s writer window, then a full tester window — and died at the soft limit
with a billiard exception instead of a named phase (371/372). The deadline
must be TASK-scoped (approval resume runs in a NEW task — a created_at-based
clock would insta-kill every resumed job), recomputed per-invoke (no
check-then-expire race), published to tools via ``set_tool_deadline``, and
never hand a phase 0 seconds silently (finalize margin + named breach).

Contract:
- ``_effective_timeout(phase_timeout, job_deadline, now)`` (pure, clock
  injected): None deadline → passthrough; healthy budget → min() clamp;
  exhausted budget → 0.0 + named breach; clamps are NAMED (loggable);
- the task entry stamps ``task_deadline`` into the initial graph state
  (run + resume paths);
- ``_invoke_agent_with_timeout`` recomputes the effective timeout from the
  context state's ``task_deadline`` and publishes the CLAMPED deadline to
  tools.
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

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402


NOW = 1_000_000.0
DEADLINE = NOW + 12_960.0  # the wave-22 soft limit


class TestEffectiveTimeoutMath:
    def test_no_deadline_is_passthrough(self):
        assert graph._effective_timeout(900.0, None, NOW) == (900.0, None)
        assert graph._effective_timeout(900.0, 0, NOW) == (900.0, None)

    def test_healthy_budget_clamps_to_phase_timeout(self):
        # 12,000s of budget left, phase wants 900 → no clamp, no breach
        assert graph._effective_timeout(900.0, DEADLINE, NOW) == (900.0, None)

    def test_thin_budget_clamps_and_names_it(self):
        # 1,000s left, phase wants 900, margin 360 → 640s effective
        deadline = NOW + 1_000.0
        eff, reason = graph._effective_timeout(900.0, deadline, NOW)
        assert eff == pytest.approx(640.0)
        assert reason and "job budget" in reason

    def test_exhausted_budget_breaches_with_zero(self):
        eff, reason = graph._effective_timeout(900.0, NOW + 100.0, NOW)
        assert eff == 0.0
        assert reason and "exhausted" in reason

    def test_margin_is_reserved(self):
        # remaining exactly equals the finalize margin → nothing left to run
        eff, _ = graph._effective_timeout(
            900.0, NOW + graph.JOB_BUDGET_FINALIZE_MARGIN, NOW
        )
        assert eff == 0.0

    def test_floor_constant_is_positive(self):
        assert getattr(graph, "JOB_BUDGET_FINALIZE_MARGIN", 0) >= 60
        assert getattr(graph, "JOB_BUDGET_FLOOR", 0) > 0


class TestTaskScopedStamping:
    def test_run_task_stamps_deadline_into_config(self):
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")) as fh:
            src = fh.read()
        i = src.index("def run_scrape_task")
        j = src.index("def resume_scrape_task")
        body = src[i:j]
        assert 'config["configurable"]["task_deadline"]' in body, (
            "run_scrape_task never stamps a task-scoped deadline — phase "
            "invocations cannot clamp to the task budget"
        )

    def test_resume_path_stamps_fresh_deadline(self):
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")) as fh:
            src = fh.read()
        i = src.index("def resume_scrape_task")
        body = src[i:]
        assert 'config["configurable"]["task_deadline"]' in body, (
            "the resume path reuses a stale deadline — approval-resumed jobs "
            "would resume with ~0s of budget and insta-fail healthy work"
        )


class TestInvokePublishesClampedDeadline:
    def test_invoke_consults_task_deadline(self):
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            src = fh.read()
        i = src.index("def _invoke_agent_with_timeout")
        j = src.index("def ", i + 10)
        body = src[i:j]
        assert "_effective_timeout(" in body, (
            "_invoke_agent_with_timeout ignores the task budget"
        )
        assert "task_deadline" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
