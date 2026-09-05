"""[wave-22 A1] The celery task-limit knobs must be REAL settings.

Prod jobs 365/370/371/372 all died ``SoftTimeLimitExceeded`` at exactly 3h:
``tasks.py`` reads ``CELERY_TASK_SOFT_TIME_LIMIT`` via ``getattr(settings, …,
10800)``, but settings.py NEVER DEFINES the attribute — so the documented
"tune via settings" knob was silently dead everywhere it was set, and the
default ceiling (3h) sat BELOW a legal execution window
(``EXECUTION_MAX_TIMEOUT=9600`` + observed pre-exec phases ≈ 12.3k s). The
soft limit fired mid-execution and the billiard headline became the job's
error message.

Contract:
- ``settings.CELERY_TASK_SOFT_TIME_LIMIT`` / ``CELERY_TASK_TIME_LIMIT`` exist
  as real attributes (env-overridable via django-environ ``config()``);
- the composition invariant holds: ``EXECUTION_MAX_TIMEOUT < soft < hard``
  (a full-ceiling execution plus finalize grace must fit);
- ``tasks.py``'s module-level ``_RUN_TASK_*`` read the settings verbatim —
  no getattr-fallback drift between the two modules.
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
from django.conf import settings  # noqa: E402


class TestTaskLimitKnobsAreReal:
    def test_soft_limit_attr_exists(self):
        assert hasattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT"), (
            "settings.py never defines CELERY_TASK_SOFT_TIME_LIMIT — the "
            "tasks.py getattr fallback (3h) is the only value that can ever "
            "be in force, and env tuning is silently dead (jobs 371/372)"
        )

    def test_hard_limit_attr_exists(self):
        assert hasattr(settings, "CELERY_TASK_TIME_LIMIT")

    def test_values_are_positive_ints(self):
        assert int(settings.CELERY_TASK_SOFT_TIME_LIMIT) > 0
        assert int(settings.CELERY_TASK_TIME_LIMIT) > 0


class TestCompositionInvariant:
    def test_execution_max_fits_below_soft_limit(self):
        # A ceiling-capped execution (9600s) plus the observed worst healthy
        # pre-exec window (~2700s: probe ladder + 3-4 LLM phases + tester)
        # plus finalize grace (360s) must fit under the soft limit.
        assert settings.EXECUTION_MAX_TIMEOUT < int(
            settings.CELERY_TASK_SOFT_TIME_LIMIT
        ), (
            "EXECUTION_MAX_TIMEOUT exceeds the celery soft limit — a legal "
            "max-length execution would be soft-killed mid-run"
        )

    def test_soft_below_hard(self):
        assert int(settings.CELERY_TASK_SOFT_TIME_LIMIT) < int(
            settings.CELERY_TASK_TIME_LIMIT
        ), "hard limit must exceed soft (the finalize-grace window)"

    def test_ceiling_accommodates_full_pipeline(self):
        # 9600 exec + 2700 pre-exec + 360 grace = 12660 — the soft default
        # must clear it (critique-8 constraint math), unlike the old 10800.
        assert int(settings.CELERY_TASK_SOFT_TIME_LIMIT) >= 12660


class TestTasksModuleReadsSettings:
    def test_tasks_module_values_match_settings(self):
        tasks = importlib.import_module("scraper.tasks")
        assert int(tasks._RUN_TASK_SOFT_TIME_LIMIT) == int(
            settings.CELERY_TASK_SOFT_TIME_LIMIT
        ), (
            "tasks.py's _RUN_TASK_SOFT_TIME_LIMIT drifted from settings — "
            "the decorator bakes the limit at import time, so any drift "
            "silently overrides the configured ceiling"
        )
        assert int(tasks._RUN_TASK_TIME_LIMIT) == int(
            settings.CELERY_TASK_TIME_LIMIT
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
