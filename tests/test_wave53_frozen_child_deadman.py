"""[wave-53 job-538] Frozen-child deadman — the out-of-process liveness kill.

What happened (proven RCA, job 538 countryroad 2026-10-02): product_analyzer's
model call #50 started at 20:43:44 IST and the worker child PROCESS never
logged again — no z.ai response line, no classified-retry line, and BOTH
in-process timer families died in the same second: the invocation-level
``thread.join(900)`` (due to expire-and-log at ~20:45:46) and the independent
``threading.Timer`` heartbeat chain (beat due 2 min later). A mere hung socket
cannot explain that — only a deadlocked process can; every in-process defense
(httpx read timeout, no-chunk watchdog, join, heartbeat) executes no Python
once the process is frozen. The stuck-job watchdog finally killed it 109 min
in, bounded only by ``ACTIVE_SILENCE_REVOKE_MINUTES=90`` plus tick cadence.

The fix cannot live in-process (a frozen process runs no timeout). It lives in
the one defender that survived: the out-of-process watchdog on the events
queue. Signal: a live phase ALWAYS emits a SessionLog row at least every 300s
(``[HEARTBEAT]`` rows during agent phases, ``[EXEC-ALIVE]`` during execution,
``[TOOL]``/``[PROBE]`` rows otherwise). Total silence across ALL rows —
heartbeats included — for ≥ ``FROZEN_CHILD_SILENCE_MINUTES`` while Celery
still reports the task ACTIVE is therefore not a "long quiet phase": it is a
frozen child, and it gets the named failure + SIGKILL revoke immediately
instead of aging to 90 minutes.

The existing 30/90 ladder stays for the quiet-but-alive class (real activity
stale, heartbeats fresh): the deadman only opens when BOTH streams are stale.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_running_job(minutes_ago_started=60):
    """A RUNNING job with started_at stamped and a celery task id."""
    from django.utils import timezone

    from scraper.models import ScrapeJob

    job = ScrapeJob.objects.create(
        url="https://example.com/collections/jackets",
        status=ScrapeJob.STATUS_RUNNING,
        celery_task_id="frozen-task-538",
    )
    ScrapeJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - timezone.timedelta(
            minutes=minutes_ago_started
        ),
        created_at=timezone.now() - timezone.timedelta(
            minutes=minutes_ago_started
        ),
    )
    job.refresh_from_db()
    return job


def _log(job, content, minutes_ago, agent="product-analyzer"):
    """created_at is auto_now_add — backdate via a queryset update."""
    from django.utils import timezone

    from scraper.models import SessionLog

    row = SessionLog.objects.create(
        job=job,
        role=SessionLog.ROLE_SYSTEM,
        agent=agent,
        content=content,
        seq=SessionLog.objects.filter(job=job).count(),
    )
    SessionLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timezone.timedelta(minutes=minutes_ago)
    )
    row.refresh_from_db()
    return row


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """The kill tail must not touch the broker or browser_service in tests."""
    revocations = []

    def fake_revoke(task_id, terminate=False, signal=None, **kw):
        revocations.append((task_id, terminate, signal))

    monkeypatch.setattr(
        "celery.current_app.control.revoke", fake_revoke, raising=False
    )
    monkeypatch.setattr(
        "agents.tools.browser_http.cancel_scrape",
        lambda job_id: {"requested": True, "flagged": []},
        raising=False,
    )
    monkeypatch.setattr("django.db.close_old_connections", lambda: None)
    return revocations


def _set_liveness(monkeypatch, verdict):
    import scraper.tasks as wt

    monkeypatch.setattr(wt, "_task_liveness", lambda task_id: verdict)


def _run_watchdog():
    import scraper.tasks as wt

    wt.cleanup_stuck_jobs.__wrapped__()


# ─── the deadman ─────────────────────────────────────────────────────────────


class TestFrozenChildDeadman:
    @pytest.mark.django_db
    def test_total_silence_with_live_task_is_killed_now(self, monkeypatch):
        """The exact 538 shape: real rows AND heartbeats both stop at the
        same moment, the celery parent still reports the task ACTIVE. The
        watchdog must name the freeze and SIGKILL-revoke instead of aging
        to the 90-min wedge backstop."""
        from scraper.models import ScrapeJob

        job = _make_running_job()
        _log(job, "[TOOL] playwright_browser_evaluate: {...}", 40)
        _log(job, "[HEARTBEAT] Agent product-analyzer still running...", 40)
        _set_liveness(monkeypatch, "active")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "frozen" in (job.error_message or "").lower()
        assert "ACTIVE" in (job.error_message or "")

    @pytest.mark.django_db
    def test_deadman_revoke_is_terminate_sigkill(
        self, _no_side_effects, monkeypatch
    ):
        job = _make_running_job()
        _log(job, "[TOOL] read_file: {...}", 40)
        _log(job, "[HEARTBEAT] Agent code-writer still running...", 40)
        _set_liveness(monkeypatch, "active")

        _run_watchdog()

        assert _no_side_effects == [(job.celery_task_id, True, "SIGKILL")]

    @pytest.mark.django_db
    def test_fresh_heartbeat_rescues_a_quiet_llm_phase(self, monkeypatch):
        """The quiet-but-alive class (513): real activity stale, heartbeats
        still beating — the deadman must NOT fire; the 30/90 ladder stands."""
        from scraper.models import ScrapeJob

        job = _make_running_job()
        _log(job, "[TOOL] playwright_browser_evaluate: {...}", 40)
        _log(job, "[HEARTBEAT] Agent product-analyzer still running...", 5)
        _set_liveness(monkeypatch, "active")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING

    @pytest.mark.django_db
    def test_total_silence_below_the_deadman_is_untouched(self, monkeypatch):
        from scraper.models import ScrapeJob

        job = _make_running_job()
        _log(job, "[TOOL] read_file: {...}", 12)
        _log(job, "[HEARTBEAT] Agent site-analyzer still running...", 12)
        _set_liveness(monkeypatch, "active")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_RUNNING

    @pytest.mark.django_db
    def test_absent_liveness_takes_the_honest_worker_lost_path(self, monkeypatch):
        """Total silence + a task that runs NOWHERE is the existing corpse
        class — same kill, different (honest) name. The deadman must not
        mask it."""
        from scraper.models import ScrapeJob

        job = _make_running_job()
        _log(job, "[TOOL] read_file: {...}", 40)
        _log(job, "[HEARTBEAT] Agent product-analyzer still running...", 40)
        _set_liveness(monkeypatch, "absent")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "Worker process lost" in (job.error_message or "")

    @pytest.mark.django_db
    def test_unknown_liveness_keeps_the_silence_rule(self, monkeypatch):
        """An inspect outage must not upgrade to the frozen verdict (the
        jobs-79/80 lesson: silence ≠ proof)."""
        from scraper.models import ScrapeJob

        job = _make_running_job()
        _log(job, "[TOOL] read_file: {...}", 40)
        _log(job, "[HEARTBEAT] Agent product-analyzer still running...", 40)
        _set_liveness(monkeypatch, "unknown")

        _run_watchdog()

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "frozen" not in (job.error_message or "").lower()


# ─── wiring contracts ────────────────────────────────────────────────────────


class TestDeadmanWiring:
    def test_constant_is_below_the_wedge_backstop(self):
        import scraper.tasks as wt

        assert wt.FROZEN_CHILD_SILENCE_MINUTES < wt.ACTIVE_SILENCE_REVOKE_MINUTES

    def test_deadman_runs_before_the_active_grace(self):
        """Source contract: the deadman check sits BEFORE the
        ``idle_minutes < ACTIVE_SILENCE_REVOKE_MINUTES: continue`` grace —
        a grace-skip upstream of the deadman would make it unreachable for
        the 15-90 min window it exists for."""
        import inspect

        import scraper.tasks as wt

        src = inspect.getsource(wt.cleanup_stuck_jobs)
        deadman_at = src.index("FROZEN_CHILD_SILENCE_MINUTES")
        grace_at = src.index("ACTIVE_SILENCE_REVOKE_MINUTES")
        assert deadman_at < grace_at

    def test_deadman_message_is_greppable_for_postmortems(self):
        """The message must name the mechanism (process deadlock, in-process
        timers dead) so a postmortem grep lands on the RCA, not a vague
        'wedged scrape'."""
        import inspect

        import scraper.tasks as wt

        src = inspect.getsource(wt.cleanup_stuck_jobs)
        assert "deadlocked" in src or "deadlock" in src
        assert "in-process timer" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
