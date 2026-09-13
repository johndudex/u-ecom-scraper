"""[wave-30 W30-9] Step finalizer: never-run ≠ done.

The graph-finalizer closed every still-open Step row as DONE. But a PENDING
step at job end is a phase that NEVER STARTED — resume skips, early deaths,
skipped deterministic nodes — and the UI showed "Done" for work that never
happened. Labeled cosmetic by the wave-26 audit (no wall-clock or money at
stake), so the contract is minimal:

1. PENDING (never started) finalizes SKIPPED — a new honest status.
2. RUNNING steps keep today's contract: the phase began, so DONE stands.
3. DONE steps are untouched.
4. The finalizer calls the closer (static pin — one vocabulary, no inline
   drift between the extracted helper and the finalize path).
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
from scraper.models import ScrapeJob, Step  # noqa: E402


@pytest.fixture()
def job_with_steps(db):
    job = ScrapeJob.objects.create(url="https://s.example/")
    Step.objects.create(job=job, phase="site_analysis", status=Step.STATUS_DONE)
    Step.objects.create(job=job, phase="testing", status=Step.STATUS_PENDING)
    Step.objects.create(job=job, phase="execution", status=Step.STATUS_RUNNING)
    return job


class TestSkippedStatus:
    def test_model_defines_skipped(self):
        assert Step.STATUS_SKIPPED == "skipped"
        assert any(k == "skipped" for k, _ in Step.STATUS_CHOICES), (
            "skipped must be a first-class choice so the UI/API render it"
        )


@pytest.mark.django_db
class TestCloseOpenSteps:
    def test_never_run_finalizes_skipped(self, job_with_steps):
        from scraper.tasks import _close_open_steps

        _close_open_steps(job_with_steps)
        testing = job_with_steps.steps.get(phase="testing")
        assert testing.status == Step.STATUS_SKIPPED, (
            "a step that never started must not read as done — never-run ≠ done"
        )
        assert testing.completed_at is not None

    def test_running_steps_still_finalize_done(self, job_with_steps):
        from scraper.tasks import _close_open_steps

        _close_open_steps(job_with_steps)
        assert job_with_steps.steps.get(phase="execution").status == Step.STATUS_DONE

    def test_done_steps_untouched(self, job_with_steps):
        from scraper.tasks import _close_open_steps

        before = job_with_steps.steps.get(phase="site_analysis")
        _close_open_steps(job_with_steps)
        after = job_with_steps.steps.get(phase="site_analysis")
        assert after.status == Step.STATUS_DONE
        assert after.pk == before.pk

    def test_finalize_path_uses_the_closer(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        i_fn = src.index("def _finalize_job")
        tail = src[i_fn:]
        i_close = tail.index("def _close_open_steps")
        i_call = tail.index("_close_open_steps(job)")
        assert i_call > i_close, (
            "the inline step-closing block must be the shared helper, called "
            "from the finalize path"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
