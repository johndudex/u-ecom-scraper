"""[wave-56b F6b] The activity extension must not eat the tester's window
floor.

Deep-audit proof (2026-10-09): ``_join_with_activity_extension`` caps the
extension at ``start + max_timeout`` but never reserves the tester window
(``TESTER_WINDOW_FLOOR_S``), so a writer that keeps the log fresh can hold
the job until the tester's guaranteed 900 s is gone — exactly the starvation
wave-56's floor was built to prevent (wonderbly job 580 shape). The join
gains a ``reserve_s`` term; the writer call site passes the floor whenever
a writer death has already been recorded.
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from webapp.agents.graph import JOB_BUDGET_FINALIZE_MARGIN, _join_with_activity_extension


class _FreshActivity:
    last_activity = 0.0

    def touch(self):
        self.last_activity = time.monotonic()


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        # Advance one simulated second per read so the join's poll loop can
        # actually reach its deadlines (a frozen clock made the loop spin
        # until the harness thread's 30s suicide — vacuous passes).
        self.t += 1.0
        return self.t


def _run_join(clock, activity, *, reserve_s, job_deadline, base_timeout=600.0,
              fresh_s=600.0, max_timeout=2700.0, poll_s=0.0):
    """A thread that outlives everything; join returns whatever the wait
    decided."""
    started = threading.Event()
    release = threading.Event()

    def _body():
        started.set()
        release.wait(30)

    th = threading.Thread(target=_body)
    th.start()
    started.wait(5)
    try:
        waited = _join_with_activity_extension(
            th,
            base_timeout=base_timeout,
            activity=activity,
            fresh_s=fresh_s,
            max_timeout=max_timeout,
            clock=clock,
            job_deadline=job_deadline,
            poll_s=poll_s,
            reserve_s=reserve_s,
        )
    finally:
        release.set()
        th.join(10)
    return waited


class TestReserveFloor:
    def test_reserve_truncates_the_extension(self):
        """Remaining-after-margin is 1000 s; a 900 s reserve leaves only
        ~100 s of writer wait even with a continuously-fresh log."""
        clock = _FakeClock()
        activity = _FreshActivity()
        job_deadline = clock() + JOB_BUDGET_FINALIZE_MARGIN + 1000.0

        def tick():
            clock.t += 5.0
            activity.touch()

        old = _join_with_activity_extension  # noqa: F841
        waited = _run_join(clock, activity, reserve_s=900.0,
                           job_deadline=job_deadline, poll_s=0.0)
        # The loop may exit one poll past the boundary; far short of 600 s.
        assert waited <= 100.0 + 50.0, f"waited={waited}"

    def test_no_reserve_keeps_current_extension(self):
        clock = _FakeClock()
        activity = _FreshActivity()
        job_deadline = clock() + JOB_BUDGET_FINALIZE_MARGIN + 1000.0
        waited = _run_join(clock, activity, reserve_s=0.0,
                           job_deadline=job_deadline, poll_s=0.0)
        assert waited >= 590.0, f"waited={waited}"

    def test_no_job_deadline_ignores_reserve(self):
        clock = _FakeClock()
        activity = _FreshActivity()
        waited = _run_join(clock, activity, reserve_s=900.0,
                           job_deadline=None, poll_s=0.0)
        assert waited >= 590.0, f"waited={waited}"

    def test_reserve_larger_than_window_awaits_nothing(self):
        clock = _FakeClock()
        activity = _FreshActivity()
        job_deadline = clock() + JOB_BUDGET_FINALIZE_MARGIN + 500.0
        waited = _run_join(clock, activity, reserve_s=900.0,
                           job_deadline=job_deadline, poll_s=0.0)
        assert waited <= 50.0, f"waited={waited}"
