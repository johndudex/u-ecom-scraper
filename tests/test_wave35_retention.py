"""[wave-35] DB retention — purge heavy rows behind dead-end jobs.

Locks:
- only failed/cancelled (short window) + completed (long window) are purged
- parked/resumable statuses are NEVER touched (pending, running,
  waiting_approval, captcha_blocked, akamai_blocked, browser_unavailable) —
  resumable jobs need their checkpoints; models._TERMINAL_JOB_STATUSES
  deliberately counts captcha/akamai as terminal, retention must NOT
- ScrapeJob rows + JobListing rows survive (history intact)
- the id cursor terminates the loop when job rows persist (batch_size <
  expired count)
- checkpoint tables absent (fresh DB) → skipped gracefully, not a crash
- dry-run deletes nothing but reports
- django expired sessions fold into the same sweep
- beat entry + events routing + settings knobs wired
"""

from __future__ import annotations

import os
import sys
from io import StringIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.sessions.models import Session  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from scraper import models  # noqa: E402
from scraper.retention import purge_retention  # noqa: E402

from config import settings as dj_settings  # noqa: E402

OLD = timezone.now() - timezone.timedelta(days=30)
DAYS_100 = timezone.now() - timezone.timedelta(days=100)
FRESH = timezone.now() - timezone.timedelta(days=1)
DAYS_10 = timezone.now() - timezone.timedelta(days=10)

_seed_seq = 0


@pytest.fixture
def checkpoint_tables():
    """Minimal langgraph-shaped checkpoint tables. Created inside the test
    transaction (pytest-django TestCase), so the DDL rolls back at teardown —
    no cleanup needed, and the tables never leak into other tests."""
    with connection.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id text NOT NULL,
                checkpoint_ns text NOT NULL DEFAULT '',
                checkpoint_id text NOT NULL,
                parent_checkpoint_id text,
                checkpoint text,
                metadata text,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoint_blobs (
                thread_id text NOT NULL,
                checkpoint_ns text NOT NULL DEFAULT '',
                channel text NOT NULL,
                version text NOT NULL,
                type text,
                blob bytea,
                PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoint_writes (
                thread_id text NOT NULL,
                checkpoint_ns text NOT NULL DEFAULT '',
                checkpoint_id text NOT NULL,
                task_id text NOT NULL DEFAULT '',
                idx integer NOT NULL,
                channel text NOT NULL,
                type text,
                blob bytea,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id,
                             task_id, idx)
            )
            """
        )
    yield


def _seed_job(status: str, created_at, **kw) -> models.ScrapeJob:
    """A job with one row in each heavy child table, backdated to
    ``created_at`` (auto_now_add stamps insert time — the update bypasses
    it, matching how the prod rows actually age)."""
    global _seed_seq
    _seed_seq += 1
    job = models.ScrapeJob.objects.create(
        url=kw.pop("url", f"https://example.com/p/{status}-{_seed_seq}"),
        status=status,
        input_mode="url_list",
        page_type="product",
        **kw,
    )
    if created_at is not None:
        models.ScrapeJob.objects.filter(pk=job.pk).update(created_at=created_at)
    models.SessionLog.objects.create(job=job, role="tool", content="x" * 2000, seq=1)
    models.ToolCallLog.objects.create(job=job, agent="site_analyzer", tool_name="probe")
    models.Step.objects.create(
        job=job, phase="site_analysis", status=models.Step.STATUS_DONE
    )
    return job


def _seed_checkpoints(job_ids, *, rows_per_table: int = 2) -> None:
    with connection.cursor() as cur:
        for jid in job_ids:
            thread = f"job-{jid}"
            for i in range(rows_per_table):
                cur.execute(
                    "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint)"
                    " VALUES (%s, %s, %s)",
                    [thread, f"cp-{i}", "state"],
                )
                cur.execute(
                    "INSERT INTO checkpoint_blobs (thread_id, channel, version, blob)"
                    " VALUES (%s, %s, %s, %s)",
                    [thread, "state", f"v-{i}", "x" * 500],
                )
                cur.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, idx,"
                    " channel) VALUES (%s, %s, %s, %s)",
                    [thread, f"cp-{i}", i, "state"],
                )


def _checkpoint_thread_count(thread: str) -> int:
    with connection.cursor() as cur:
        total = 0
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", [thread])
            total += cur.fetchone()[0]
        return total


def _child_count(job: models.ScrapeJob) -> int:
    return (
        job.session_logs.count()
        + job.tool_call_logs.count()
        + job.steps.count()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Core purge semantics
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
class TestPurgeSemantics:
    def test_purges_expired_deadends_spares_everything_else(
        self, checkpoint_tables
    ):
        old_failed = _seed_job("failed", OLD)
        old_cancelled = _seed_job("cancelled", OLD)
        old_completed = _seed_job("completed", DAYS_100)
        fresh_failed = _seed_job("failed", FRESH)
        fresh_completed = _seed_job("completed", FRESH)
        _seed_checkpoints([old_failed.id, old_cancelled.id, old_completed.id,
                           fresh_failed.id, fresh_completed.id])

        report = purge_retention(days_failed=7, days_completed=90, batch_size=2)

        assert report["jobs_purged"] == 3
        assert report["dry_run"] is False
        for job in (old_failed, old_cancelled, old_completed):
            assert _checkpoint_thread_count(f"job-{job.id}") == 0
            assert _child_count(job) == 0
        # fresh rows survive both windows
        for job in (fresh_failed, fresh_completed):
            assert _checkpoint_thread_count(f"job-{job.id}") > 0
            assert _child_count(job) > 0
        # history intact — the job rows themselves survive
        assert models.ScrapeJob.objects.filter(
            pk__in=[old_failed.pk, old_cancelled.pk, old_completed.pk]
        ).count() == 3

    def test_parked_and_resumable_statuses_never_purged(self, checkpoint_tables):
        parked = [
            _seed_job(status, OLD)
            for status in (
                "pending",
                "running",
                "waiting_approval",
                "captcha_blocked",
                "akamai_blocked",
                "browser_unavailable",
            )
        ]
        _seed_checkpoints([j.id for j in parked])

        report = purge_retention(days_failed=0, days_completed=0)

        assert report["jobs_purged"] == 0
        for job in parked:
            assert _checkpoint_thread_count(f"job-{job.id}") > 0
            assert _child_count(job) > 0

    def test_windows_are_independent(self, checkpoint_tables):
        """failed/cancelled use the short window; completed the long one."""
        failed_10d = _seed_job("failed", DAYS_10)
        completed_10d = _seed_job("completed", DAYS_10)
        completed_100d = _seed_job("completed", DAYS_100)

        purge_retention(days_failed=7, days_completed=90, batch_size=5)

        assert _child_count(failed_10d) == 0, "failed past 7d must go"
        assert _child_count(completed_10d) > 0, "completed at 10d < 90d stays"
        assert _child_count(completed_100d) == 0, "completed past 90d goes"

    def test_id_cursor_terminates_when_job_rows_persist(self, checkpoint_tables):
        """The purge keeps ScrapeJob rows, so a naive 'first N' loop would
        re-claim the same page forever. batch_size < expired must still
        drain every expired job."""
        expired = [_seed_job("failed", OLD) for _ in range(5)]
        _seed_checkpoints([j.id for j in expired])

        report = purge_retention(days_failed=7, days_completed=90, batch_size=2)

        assert report["jobs_purged"] == 5
        for job in expired:
            assert _child_count(job) == 0

    def test_absent_checkpoint_tables_skipped_not_fatal(self):
        """Fresh databases have no checkpoint tables (langgraph creates them
        at runtime). The sweep must still purge the ORM child tables."""
        with connection.cursor() as cur:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # transactional
        old_failed = _seed_job("failed", OLD)
        report = purge_retention(days_failed=7, days_completed=90)
        assert report["checkpoint_tables_present"] is False
        assert report["jobs_purged"] == 1
        assert _child_count(old_failed) == 0

    def test_dry_run_reports_without_deleting(self, checkpoint_tables):
        old_failed = _seed_job("failed", OLD)
        _seed_checkpoints([old_failed.id])

        report = purge_retention(days_failed=7, days_completed=90, dry_run=True)

        assert report["jobs_expired"] == 1
        assert report["checkpoint_threads"] == 2
        assert report["sessionlog_rows"] == 1
        assert _child_count(old_failed) > 0
        assert _checkpoint_thread_count(f"job-{old_failed.id}") > 0

    def test_django_sessions_folded_in(self, checkpoint_tables):
        from datetime import timedelta

        Session.objects.create(
            session_key="expired1", session_data="x",
            expire_date=timezone.now() - timedelta(days=1),
        )
        Session.objects.create(
            session_key="live1", session_data="x",
            expire_date=timezone.now() + timedelta(days=1),
        )

        report = purge_retention(days_failed=7, days_completed=90)

        assert report["django_sessions_purged"] == 1
        assert not Session.objects.filter(session_key="expired1").exists()
        assert Session.objects.filter(session_key="live1").exists()


# ═══════════════════════════════════════════════════════════════════════════
# Wiring: task, beat, route, settings, management command
# ═══════════════════════════════════════════════════════════════════════════


class TestWiring:
    def test_beat_entry_registered_and_offpeak(self):
        entry = dj_settings.CELERY_BEAT_SCHEDULE["purge-retention"]
        assert entry["task"] == "scraper.tasks.purge_retention"
        sched = entry["schedule"]
        # crontab off-peak (03:17) — not a 300.0 poller
        assert sched.hour == {3}
        assert sched.minute == {17}

    def test_task_routes_to_events_pool(self):
        assert (
            dj_settings.CELERY_TASK_ROUTES["scraper.tasks.purge_retention"]
            == "events"
        )

    def test_default_windows(self):
        assert dj_settings.RETENTION_DAYS_FAILED == 7
        assert dj_settings.RETENTION_DAYS_COMPLETED == 90
        assert dj_settings.RETENTION_ENABLED is True

    @override_settings(RETENTION_ENABLED=False)
    @pytest.mark.django_db
    def test_task_honors_kill_switch(self):
        from scraper.tasks import purge_retention as task

        assert task.apply().get() == {"disabled": True}

    @pytest.mark.django_db
    def test_task_runs_with_settings_windows(self, checkpoint_tables):
        from scraper.tasks import purge_retention as task

        old = _seed_job("failed", OLD)
        report = task.apply().get()
        assert report["jobs_purged"] == 1
        assert _child_count(old) == 0


@pytest.mark.django_db
class TestManagementCommand:
    def test_dry_run_by_default(self, checkpoint_tables, capsys):
        old_failed = _seed_job("failed", OLD)
        out = StringIO()
        call_command("purge_retention", stdout=out)
        text = out.getvalue()
        assert "DRY RUN" in text
        assert "jobs expired: 1" in text
        assert _child_count(old_failed) > 0

    def test_write_applies(self, checkpoint_tables):
        old_failed = _seed_job("failed", OLD)
        out = StringIO()
        call_command("purge_retention", "--write", stdout=out)
        assert _child_count(old_failed) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
